"""
All AWS infrastructure for the Banking Fraud Detection system, defined with AWS CDK (Python).

  Internet -> ALB -> ECS Fargate (API) -> SQS -> Lambda -> DynamoDB + SNS (+ Firehose -> S3)
                          |
                          +-> DynamoDB (account state)

Settings come from cdk.json "context" and can be overridden on the command line:
  cdk deploy -c alert_email=you@example.com -c enable_firehose=true
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from aws_cdk.aws_ecr_assets import Platform
from constructs import Construct

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FraudDetectionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alert_email = (self.node.try_get_context("alert_email") or "").strip()
        enable_firehose = str(self.node.try_get_context("enable_firehose")).lower() == "true"
        alarm_threshold = int(self.node.try_get_context("fraud_alarm_threshold") or 10)
        demo_password = self.node.try_get_context("demo_password") or "Demo@1234"

        # ------------------------------------------------------------------
        # Networking: 2 availability zones, public subnets only.
        # No NAT gateway (saves ~$35/month). Tasks get public IPs for outbound
        # calls to AWS, but their security group only accepts traffic from the ALB.
        # ------------------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
        )

        # ------------------------------------------------------------------
        # DynamoDB tables
        # ------------------------------------------------------------------
        account_state_table = dynamodb.Table(
            self,
            "AccountStateTable",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,  # demo project: delete with the stack
        )

        fraud_logs_table = dynamodb.Table(
            self,
            "FraudLogsTable",
            partition_key=dynamodb.Attribute(name="transaction_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Lets analysts query "all flagged transactions for an account, newest first"
        fraud_logs_table.add_global_secondary_index(
            index_name="by-account",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="flagged_at", type=dynamodb.AttributeType.STRING),
        )

        # ------------------------------------------------------------------
        # SQS queue (+ dead-letter queue) decoupling the API from the Lambda
        # ------------------------------------------------------------------
        dead_letter_queue = sqs.Queue(
            self,
            "FlaggedTransactionsDLQ",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        flagged_queue = sqs.Queue(
            self,
            "FlaggedTransactionsQueue",
            visibility_timeout=Duration.seconds(120),  # 6x the Lambda timeout, per AWS guidance
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dead_letter_queue),
        )

        # ------------------------------------------------------------------
        # SNS topics: customer alerts + operations alarms
        # ------------------------------------------------------------------
        customer_alerts_topic = sns.Topic(self, "CustomerAlertsTopic", display_name="Fraud Alerts")
        ops_alarm_topic = sns.Topic(self, "OpsAlarmTopic", display_name="Fraud Ops Alarms")
        if alert_email:
            customer_alerts_topic.add_subscription(subs.EmailSubscription(alert_email))
            ops_alarm_topic.add_subscription(subs.EmailSubscription(alert_email))

        # ------------------------------------------------------------------
        # JWT signing secret, generated and stored in Secrets Manager
        # ------------------------------------------------------------------
        jwt_secret = secretsmanager.Secret(
            self,
            "JwtSigningSecret",
            description="Signing key for the fraud API's customer JWTs",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True, password_length=48
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # ECS Fargate service behind an Application Load Balancer
        # ------------------------------------------------------------------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        api_log_group = logs.LogGroup(
            self, "ApiLogs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY
        )

        api_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "TransactionApi",
            cluster=cluster,
            cpu=256,
            memory_limit_mib=512,
            desired_count=1,
            public_load_balancer=True,
            assign_public_ip=True,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                # CDK builds the Docker image from ../app and pushes it to ECR for us
                image=ecs.ContainerImage.from_asset(
                    str(PROJECT_ROOT / "app"), platform=Platform.LINUX_AMD64
                ),
                container_port=8000,
                environment={
                    "STATE_TABLE_NAME": account_state_table.table_name,
                    "FRAUD_QUEUE_URL": flagged_queue.queue_url,
                    "REQUIRE_AUTH": "true",
                    "DEMO_PASSWORD": demo_password,
                    "AWS_DEFAULT_REGION": self.region,
                },
                secrets={"JWT_SECRET": ecs.Secret.from_secrets_manager(jwt_secret)},
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=api_log_group),
            ),
        )
        api_service.target_group.configure_health_check(
            path="/health", healthy_http_codes="200", interval=Duration.seconds(30)
        )
        api_service.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")

        # Least-privilege permissions for the API's task role
        task_role = api_service.task_definition.task_role
        flagged_queue.grant_send_messages(task_role)
        account_state_table.grant_read_write_data(task_role)

        # ------------------------------------------------------------------
        # Lambda: notification + record keeping
        # ------------------------------------------------------------------
        notifier_log_group = logs.LogGroup(
            self, "NotifierLogs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY
        )
        notifier = _lambda.Function(
            self,
            "FraudNotifier",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(PROJECT_ROOT / "lambda" / "notifier")),
            timeout=Duration.seconds(20),
            memory_size=256,
            log_group=notifier_log_group,
            environment={
                "FRAUD_TABLE_NAME": fraud_logs_table.table_name,
                "ALERT_TOPIC_ARN": customer_alerts_topic.topic_arn,
            },
        )
        notifier.add_event_source(
            lambda_events.SqsEventSource(flagged_queue, batch_size=10, report_batch_item_failures=True)
        )
        fraud_logs_table.grant_write_data(notifier)
        customer_alerts_topic.grant_publish(notifier)

        # ------------------------------------------------------------------
        # BONUS: Kinesis Data Firehose -> S3 data lake for future ML (opt-in)
        # ------------------------------------------------------------------
        if enable_firehose:
            data_lake = s3.Bucket(
                self,
                "FlaggedTransactionsDataLake",
                encryption=s3.BucketEncryption.S3_MANAGED,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
            firehose_role = iam.Role(
                self, "FirehoseRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com")
            )
            data_lake.grant_write(firehose_role)

            delivery_stream = firehose.CfnDeliveryStream(
                self,
                "FlaggedTransactionsStream",
                delivery_stream_type="DirectPut",
                extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                    bucket_arn=data_lake.bucket_arn,
                    role_arn=firehose_role.role_arn,
                    prefix="flagged/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
                    error_output_prefix="errors/!{firehose:error-output-type}/",
                    buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                        interval_in_seconds=60, size_in_m_bs=1
                    ),
                    compression_format="GZIP",
                ),
            )
            delivery_stream.node.add_dependency(firehose_role)

            notifier.add_environment("FIREHOSE_STREAM_NAME", delivery_stream.ref)
            notifier.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                    resources=[delivery_stream.attr_arn],
                )
            )
            CfnOutput(self, "DataLakeBucket", value=data_lake.bucket_name)

        # ------------------------------------------------------------------
        # BONUS: CloudWatch alarms
        # ------------------------------------------------------------------
        fraud_spike_alarm = cloudwatch.Alarm(
            self,
            "FraudSpikeAlarm",
            alarm_description=(
                f"{alarm_threshold}+ transactions flagged in 5 minutes - possible fraud attack "
                "or a misconfigured rule"
            ),
            metric=flagged_queue.metric_number_of_messages_sent(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=alarm_threshold,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        fraud_spike_alarm.add_alarm_action(cw_actions.SnsAction(ops_alarm_topic))

        dlq_alarm = cloudwatch.Alarm(
            self,
            "NotificationFailuresAlarm",
            alarm_description="Flagged transactions failed processing and landed in the dead-letter queue",
            metric=dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(ops_alarm_topic))

        # ------------------------------------------------------------------
        # Outputs printed after `cdk deploy`
        # ------------------------------------------------------------------
        api_url = f"http://{api_service.load_balancer.load_balancer_dns_name}"
        CfnOutput(self, "ApiUrl", value=api_url, description="Base URL of the fraud API")
        CfnOutput(self, "ApiDocsUrl", value=f"{api_url}/docs", description="Interactive Swagger UI")
        CfnOutput(self, "FraudLogsTableName", value=fraud_logs_table.table_name)
        CfnOutput(self, "FlaggedQueueUrl", value=flagged_queue.queue_url)
