// KiroCrew on a Lambda MicroVM whose OpenCode harness runs remotely in a Lambda Web Function (see crew/README.md).
// A separate stack from KiroCrewMicrovmStack (the first design), which stays untouched until this one passes the same matrix.
// Nothing here bills until a crew is launched with crew/scripts/crew.sh.
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3assets from "aws-cdk-lib/aws-s3-assets";
import { fileURLToPath } from "node:url";

const IMAGE_NAME = "kirocrew-remote";
const BASE_IMAGE = "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1";
const INTERNET_EGRESS = "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS";
const KEY_PARAM = process.env.NIM_KEY_PARAM ?? "/nim-free/api-key"; // Standard-tier String parameter you create first (see README); the stack only reads it
const STATE_PREFIX = "crew/home/";
// The Web Function that runs OpenCode (webfn/, stack OpenCodeWebFnStack). A crew may invoke this one endpoint and nothing else.
const WEBFN_NAME = "kirocrew-opencode";
const WEBFN_ENDPOINT = "default";

class KiroCrewRemoteCrewStack extends cdk.Stack {
  constructor(scope: cdk.App, id: string, props: cdk.StackProps) {
    super(scope, id, props);
    const lambdaService = () =>
      new iam.ServicePrincipal("lambda.amazonaws.com", { conditions: { StringEquals: { "aws:SourceAccount": this.account } } });
    const logGroup = new logs.LogGroup(this, "Logs", {
      logGroupName: `/aws/lambda-microvms/${IMAGE_NAME}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const recipe = new s3assets.Asset(this, "Recipe", { path: fileURLToPath(new URL("./image", import.meta.url)), exclude: ["**/__pycache__", "**/*.pyc"] });

    const buildRole = new iam.Role(this, "BuildRole", { assumedBy: lambdaService() });
    buildRole.addToPolicy(new iam.PolicyStatement({ actions: ["s3:GetObject"], resources: [recipe.bucket.arnForObjects(recipe.s3ObjectKey)] }));
    logGroup.grantWrite(buildRole);

    // What a running crew can do: read the one NIM key parameter, write its logs, mirror its own state prefix, and call the one Web Function endpoint.
    const executionRole = new iam.Role(this, "ExecutionRole", { assumedBy: lambdaService() });
    logGroup.grantWrite(executionRole);
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["ssm:GetParameter"],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${KEY_PARAM}`],
    }));
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["lambda:InvokeWebFunctionEndpoint"],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:web-function/${WEBFN_NAME}/endpoint/${WEBFN_ENDPOINT}`],
    }));

    // The role a registered crew assumes as an SSM hybrid managed node (Remote Crew over SSM). Core agent permissions only; the
    // activation that uses it is created per launch by crew.sh, single-use and expiring in an hour. No Secrets Manager, no KMS.
    const hybridRole = new iam.Role(this, "SsmHybridRole", {
      assumedBy: new iam.ServicePrincipal("ssm.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnEquals: { "aws:SourceArn": `arn:aws:ssm:${this.region}:${this.account}:*` },
        },
      }),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore")],
    });

    // A bucket of its own: the first design's crew home must not be overwritten by this one.
    const stateBucket = new s3.Bucket(this, "State", {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [{ noncurrentVersionExpiration: cdk.Duration.days(7), abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) }],
    });
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["s3:ListBucket"], resources: [stateBucket.bucketArn], conditions: { StringLike: { "s3:prefix": [`${STATE_PREFIX}*`] } },
    }));
    executionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], resources: [stateBucket.arnForObjects(`${STATE_PREFIX}*`)],
    }));

    const image = new lambda.CfnMicrovmImage(this, "Image", {
      name: IMAGE_NAME,
      description: "KiroCrew gateway; OpenCode harness runs in a Lambda Web Function through an ACP shim",
      baseImageArn: BASE_IMAGE,
      baseImageVersion: "3",
      buildRoleArn: buildRole.roleArn,
      codeArtifact: { uri: recipe.s3ObjectUrl },
      cpuConfigurations: [{ architecture: "ARM_64" }],
      resources: [{ minimumMemoryInMiB: 2048 }],
      additionalOsCapabilities: [],
      egressNetworkConnectors: [INTERNET_EGRESS],
      environmentVariables: [],
      hooks: {
        port: 8080,
        microvmImageHooks: { ready: "ENABLED", readyTimeoutInSeconds: 120 },
        microvmHooks: {
          run: "ENABLED", runTimeoutInSeconds: 60,
          resume: "ENABLED", resumeTimeoutInSeconds: 60,
          suspend: "ENABLED", suspendTimeoutInSeconds: 60,
          terminate: "ENABLED", terminateTimeoutInSeconds: 60,
        },
      },
      logging: { cloudWatch: { logGroup: logGroup.logGroupName } },
      tags: [{ key: "Project", value: "kirocrew-on-lambda" }],
    });
    image.node.addDependency(buildRole);
    image.node.addDependency(buildRole.node.findChild("DefaultPolicy"));

    new cdk.CfnOutput(this, "ImageArn", { value: image.attrImageArn });
    new cdk.CfnOutput(this, "ExecutionRoleArn", { value: executionRole.roleArn });
    new cdk.CfnOutput(this, "LogGroup", { value: logGroup.logGroupName });
    new cdk.CfnOutput(this, "StateBucket", { value: stateBucket.bucketName });
    new cdk.CfnOutput(this, "SsmHybridRoleName", { value: hybridRole.roleName });
  }
}

// Tested in us-east-1 only: the MicroVM base image and network connector ARNs above are region-specific.
const ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT ?? (() => { throw new Error("run `aws login` or set AWS_PROFILE so CDK can see your account (CDK_DEFAULT_ACCOUNT is empty)"); })();
const app = new cdk.App();
new KiroCrewRemoteCrewStack(app, "KiroCrewRemoteCrewStack", { env: { account: ACCOUNT, region: "us-east-1" } });
