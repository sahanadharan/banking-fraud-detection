#!/usr/bin/env python3
import os

import aws_cdk as cdk

from fraud_stack import FraudDetectionStack

app = cdk.App()

FraudDetectionStack(
    app,
    "BankingFraudDetectionStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION", "us-east-2"),
    ),
    description="Banking fraud detection: ECS Fargate API, SQS, Lambda, DynamoDB, SNS",
)

cdk.Tags.of(app).add("project", "banking-fraud-detection")

app.synth()
