// OpenCode `serve` inside a Python Web Function, with conversations kept in S3. Remove with `cdk destroy`.
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { fileURLToPath } from "node:url";

class OpenCodeWebFnStack extends cdk.Stack {
  constructor(scope: cdk.App, id: string, props: cdk.StackProps) {
    super(scope, id, props);
    // Session state shared by every environment: OpenCode `export` JSON per session key. Short retention, no public access.
    const state = new s3.Bucket(this, "State", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(14) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true,
    });
    const fn = new lambda.WebFunction(this, "Fn", {
      webFunctionName: "kirocrew-opencode",
      runtime: lambda.WebFunctionRuntime.of("python3.15"),
      code: lambda.WebFunctionCode.fromAssetToManagedBucket(fileURLToPath(new URL("./dist", import.meta.url))),
      timeout: cdk.Duration.seconds(120),
      environment: { STATE_BUCKET: state.bucketName },
      defaultEndpointAuth: lambda.WebFunctionAuthType.IAM_AUTH,
      defaultEndpoint: { endpointName: "default", maxEnvironments: 10 },
    });
    fn.addToRolePolicy(new iam.PolicyStatement({ actions: ["s3:GetObject", "s3:PutObject"], resources: [state.arnForObjects("sessions/*")] }));
    fn.addToRolePolicy(new iam.PolicyStatement({ actions: ["s3:ListBucket"], resources: [state.bucketArn], conditions: { StringLike: { "s3:prefix": ["sessions/*"] } } }));
    cdk.Tags.of(this).add("Project", "kirocrew-on-lambda");
    cdk.Tags.of(this).add("Purpose", "opencode-webfunction-spike");
    new cdk.CfnOutput(this, "StateBucket", { value: state.bucketName });
    new cdk.CfnOutput(this, "Domain", { value: fn.endpoint.domainName });
  }
}
// Tested in us-east-1 only (Lambda Web Functions, MicroVMs and the python3.15 runtime preview are not available everywhere).
const ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT ?? (() => { throw new Error("run `aws login` or set AWS_PROFILE so CDK can see your account (CDK_DEFAULT_ACCOUNT is empty)"); })();
const app = new cdk.App();
new OpenCodeWebFnStack(app, "OpenCodeWebFnStack", { env: { account: ACCOUNT, region: "us-east-1" } });
