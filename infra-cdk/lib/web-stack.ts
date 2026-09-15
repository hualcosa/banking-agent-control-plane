import * as cdk from "aws-cdk-lib";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as fs from "fs";
import * as path from "path";
import { Construct } from "constructs";

export interface WebStackProps extends cdk.StackProps {
  /** AgentCore runtime ARN — must be a literal string from `-c runtimeArn=…`, not a CDK token. */
  runtimeArn: string;
}

/** SPA on S3 + CloudFront; `/api/*` proxied to the AgentCore data plane (same-origin, no CORS). */
export class WebStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);

    if (!props.runtimeArn) {
      throw new Error("cdk deploy BankingAgentWeb -c runtimeArn=<output RuntimeArn>");
    }

    const bucket = new s3.Bucket(this, "SpaBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const rewriteSource = fs.readFileSync(
      path.join(__dirname, "../functions/api-rewrite.js"),
      "utf8",
    );
    const rewriteFn = new cloudfront.Function(this, "ApiRewrite", {
      code: cloudfront.FunctionCode.fromInline(
        rewriteSource.replace("__RUNTIME_ARN_ESCAPED__", encodeURIComponent(props.runtimeArn)),
      ),
    });

    const s3Origin = origins.S3BucketOrigin.withOriginAccessControl(bucket);
    const apiOrigin = new origins.HttpOrigin("bedrock-agentcore.sa-east-1.amazonaws.com", {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
      readTimeout: cdk.Duration.seconds(60),
    });

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      defaultBehavior: {
        origin: s3Origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      additionalBehaviors: {
        "/api/*": {
          origin: apiOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          functionAssociations: [
            { function: rewriteFn, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
          ],
        },
      },
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: "/index.html" },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: "/index.html" },
      ],
    });

    new cdk.CfnOutput(this, "WebUrl", { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, "WebBucket", { value: bucket.bucketName });
    new cdk.CfnOutput(this, "DistributionId", { value: distribution.distributionId });
  }
}
