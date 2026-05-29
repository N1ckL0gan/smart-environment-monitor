"""
aws_setup.py
------------
Run ONCE to provision all AWS resources needed by the Smart Environment Monitor.

Creates:
  • DynamoDB tables  – sem_readings, sem_thresholds
  • AWS IoT Core     – Thing, Certificate, Policy, Rule → Lambda trigger
  • Amazon SNS       – Alert topic (subscribes your email)
  • Amazon Cognito   – User Pool + App Client
  • AWS Lambda       – ingest function (zips lambda/ingest.py and uploads it)
  • IAM roles        – Lambda execution role with DynamoDB/SNS/CloudWatch access

Usage:
    export AWS_REGION=ap-southeast-2
    export ALERT_EMAIL=your@email.com
    python aws_setup.py

All created resource ARNs/IDs are saved to  aws_resources.json  so the
other scripts can read them without hard-coding.
"""

import os
import json
import time
import boto3
import zipfile
import io

REGION       = os.environ.get("AWS_REGION", "ap-southeast-2")
ALERT_EMAIL  = os.environ.get("ALERT_EMAIL", "")
DEVICE_ID    = "smart-env-monitor"
ACCOUNT_ID   = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

iam       = boto3.client("iam",            region_name=REGION)
dynamo    = boto3.client("dynamodb",       region_name=REGION)
iot       = boto3.client("iot",            region_name=REGION)
sns       = boto3.client("sns",            region_name=REGION)
cognito   = boto3.client("cognito-idp",    region_name=REGION)
lam       = boto3.client("lambda",         region_name=REGION)

resources = {}


# ── DynamoDB ───────────────────────────────────────────────────────────────────

def create_dynamodb_tables():
    print("Creating DynamoDB tables …")

    for table_name, key_schema, attr_defs in [
        (
            "sem_readings",
            [
                {"AttributeName": "device_id",  "KeyType": "HASH"},
                {"AttributeName": "timestamp",  "KeyType": "RANGE"},
            ],
            [
                {"AttributeName": "device_id",  "AttributeType": "S"},
                {"AttributeName": "timestamp",  "AttributeType": "S"},
            ],
        ),
        (
            "sem_thresholds",
            [{"AttributeName": "device_id", "KeyType": "HASH"}],
            [{"AttributeName": "device_id", "AttributeType": "S"}],
        ),
    ]:
        try:
            dynamo.create_table(
                TableName=table_name,
                KeySchema=key_schema,
                AttributeDefinitions=attr_defs,
                BillingMode="PAY_PER_REQUEST",   # on-demand – no capacity planning needed
            )
            print(f"  ✓ Created {table_name}")
        except dynamo.exceptions.ResourceInUseException:
            print(f"  – {table_name} already exists, skipping")

    # Insert default thresholds
    time.sleep(15)   # let tables become ACTIVE
    db = boto3.resource("dynamodb", region_name=REGION)
    db.Table("sem_thresholds").put_item(Item={
        "device_id": DEVICE_ID,
        "temp_min": 0,   "temp_max": 40,
        "hum_min":  10,  "hum_max": 90,
        "press_min": 970, "press_max": 1030,
    })
    print("  ✓ Default thresholds inserted")
    resources["readings_table"]   = "sem_readings"
    resources["thresholds_table"] = "sem_thresholds"


# ── SNS ────────────────────────────────────────────────────────────────────────

def create_sns_topic():
    print("Creating SNS topic …")
    resp = sns.create_topic(Name="SmartEnvAlerts")
    arn  = resp["TopicArn"]
    resources["sns_topic_arn"] = arn
    print(f"  ✓ Topic ARN: {arn}")

    if ALERT_EMAIL:
        sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=ALERT_EMAIL)
        print(f"  ✓ Subscribed {ALERT_EMAIL} — check your inbox to confirm!")


# ── IAM role for Lambda ────────────────────────────────────────────────────────

def create_lambda_role() -> str:
    print("Creating Lambda IAM role …")
    role_name = "SmartEnvLambdaRole"

    assume = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":    "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action":    "sts:AssumeRole",
        }],
    })

    try:
        resp = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=assume)
        role_arn = resp["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        print(f"  – Role already exists")

    # Attach managed policies
    for policy in [
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
        "arn:aws:iam::aws:policy/AmazonSNSFullAccess",
        "arn:aws:iam::aws:policy/CloudWatchFullAccess",
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    ]:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy)

    print(f"  ✓ Role ARN: {role_arn}")
    resources["lambda_role_arn"] = role_arn
    return role_arn


# ── Lambda ─────────────────────────────────────────────────────────────────────

def deploy_lambda(role_arn: str):
    print("Deploying Lambda function …")
    fn_name = "SmartEnvIngest"

    # Zip the ingest handler
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write("lambda/ingest.py", "ingest.py")
    zip_bytes = buf.getvalue()

    env_vars = {
        "READINGS_TABLE":   "sem_readings",
        "THRESHOLDS_TABLE": "sem_thresholds",
        "SNS_TOPIC_ARN":    resources.get("sns_topic_arn", ""),
        "DEVICE_ID":        DEVICE_ID,
    }

    time.sleep(20)   # IAM role propagation delay

    try:
        resp = lam.create_function(
            FunctionName=fn_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="ingest.handler",
            Code={"ZipFile": zip_bytes},
            Timeout=30,
            Environment={"Variables": env_vars},
        )
        fn_arn = resp["FunctionArn"]
        print(f"  ✓ Created Lambda: {fn_arn}")
    except lam.exceptions.ResourceConflictException:
        resp = lam.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        fn_arn = resp["FunctionArn"]
        print(f"  – Lambda already exists, code updated")

    resources["lambda_arn"] = fn_arn
    return fn_arn


# ── IoT Core ───────────────────────────────────────────────────────────────────

def create_iot_resources(lambda_arn: str):
    print("Creating IoT Core resources …")

    # Thing
    try:
        iot.create_thing(thingName=DEVICE_ID)
        print(f"  ✓ Thing '{DEVICE_ID}' created")
    except iot.exceptions.ResourceAlreadyExistsException:
        print(f"  – Thing already exists")

    # Certificate + keys
    cert_resp = iot.create_keys_and_certificate(setAsActive=True)
    cert_arn  = cert_resp["certificateArn"]
    cert_id   = cert_resp["certificateId"]

    # Save credentials locally
    os.makedirs("certs", exist_ok=True)
    with open("certs/device-certificate.pem.crt", "w") as f:
        f.write(cert_resp["certificatePem"])
    with open("certs/private.pem.key", "w") as f:
        f.write(cert_resp["keyPair"]["PrivateKey"])

    # Download Amazon Root CA
    import urllib.request
    urllib.request.urlretrieve(
        "https://www.amazontrust.com/repository/AmazonRootCA1.pem",
        "certs/AmazonRootCA1.pem",
    )
    print("  ✓ Certificates saved to certs/")

    # IoT Policy
    policy_name = "SmartEnvPolicy"
    policy_doc  = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":   "Allow",
            "Action":   ["iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"],
            "Resource": "*",
        }],
    })
    try:
        iot.create_policy(policyName=policy_name, policyDocument=policy_doc)
    except iot.exceptions.ResourceAlreadyExistsException:
        pass

    iot.attach_policy(policyName=policy_name, target=cert_arn)
    iot.attach_thing_principal(thingName=DEVICE_ID, principal=cert_arn)
    print("  ✓ Policy attached")

# IoT Rule → Lambda
    rule_name = "SmartEnvIngestRule"
    print("  Waiting 30s for IAM role to propagate …")
    time.sleep(30)
    try:
        iot.create_topic_rule(
                    ruleName=rule_name,
                    topicRulePayload={
                        "sql":         "SELECT * FROM 'smartenv/readings'",
                        "description": "Forward sensor readings to Lambda",
                        "actions": [{
                            "lambda": {"functionArn": lambda_arn}
                        }],
                    },
                )
        print(f"  ✓ IoT Rule '{rule_name}' created")
    except Exception as e:
        if "already exists" in str(e).lower() or "ResourceAlreadyExists" in str(e):
            print(f"  – IoT Rule already exists, skipping")
        else:
            raise

    # Allow IoT to invoke Lambda
    try:
        lam.add_permission(
            FunctionName="SmartEnvIngest",
            StatementId="IoTInvokePermission",
            Action="lambda:InvokeFunction",
            Principal="iot.amazonaws.com",
        )
    except lam.exceptions.ResourceConflictException:
        pass

    endpoint = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
    resources["iot_endpoint"] = endpoint
    print(f"  ✓ IoT endpoint: {endpoint}")


# ── Cognito ────────────────────────────────────────────────────────────────────

def create_cognito():
    print("Creating Cognito User Pool …")

    pool_resp = cognito.create_user_pool(
        PoolName="SmartEnvUsers",
        Policies={"PasswordPolicy": {
            "MinimumLength": 8,
            "RequireUppercase": True,
            "RequireLowercase": True,
            "RequireNumbers": True,
            "RequireSymbols": False,
        }},
        AutoVerifiedAttributes=["email"],
        UsernameAttributes=["email"],
    )
    pool_id = pool_resp["UserPool"]["Id"]

    client_resp = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="SmartEnvWebApp",
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        GenerateSecret=False,
    )
    client_id = client_resp["UserPoolClient"]["ClientId"]

    resources["cognito_user_pool_id"] = pool_id
    resources["cognito_client_id"]    = client_id
    print(f"  ✓ User Pool ID: {pool_id}")
    print(f"  ✓ App Client ID: {client_id}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Smart Environment Monitor — AWS Setup")
    print("=" * 55)

    create_dynamodb_tables()
    create_sns_topic()
    role_arn   = create_lambda_role()
    lambda_arn = deploy_lambda(role_arn)
    create_iot_resources(lambda_arn)
    create_cognito()

    with open("aws_resources.json", "w") as f:
        json.dump(resources, f, indent=2)

    print()
    print("=" * 55)
    print("  ✅  Setup complete! Resources saved to aws_resources.json")
    print()
    print("  Next steps:")
    print("  1. Confirm your SNS email subscription")
    print("  2. Set env vars (see aws_resources.json for values):")
    print(f"       AWS_REGION={REGION}")
    print(f"       AWS_IOT_ENDPOINT=<iot_endpoint from aws_resources.json>")
    print(f"       COGNITO_USER_POOL_ID=<from aws_resources.json>")
    print(f"       COGNITO_CLIENT_ID=<from aws_resources.json>")
    print("  3. Create a Cognito user in the AWS Console or via CLI:")
    print("       aws cognito-idp admin-create-user \\")
    print(f"         --user-pool-id <pool_id> \\")
    print("         --username your@email.com")
    print("  4. Run:  python sensors.py")
    print("  5. Run:  python app.py")
    print("=" * 55)


if __name__ == "__main__":
    main()
