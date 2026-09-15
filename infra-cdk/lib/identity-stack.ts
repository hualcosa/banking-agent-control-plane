import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import { Construct } from "constructs";

/** Who the customer is, according to Cognito. The runtime's JWT authorizer and trail.app both check this pool. */
export class IdentityStack extends cdk.Stack {
  readonly userPool: cognito.UserPool;
  readonly client: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.userPool = new cognito.UserPool(this, "Customers", {
      userPoolName: "banking-agent-customers",
      selfSignUpEnabled: false,
      signInAliases: { username: true },
      passwordPolicy: { minLength: 12 },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // A public SPA client: no secret, USER_PASSWORD_AUTH for the demo login.
    this.client = this.userPool.addClient("Web", {
      generateSecret: false,
      authFlows: { userPassword: true, userSrp: true },
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
    });

    const issuer = `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}`;
    new cdk.CfnOutput(this, "UserPoolId", { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, "ClientId", { value: this.client.userPoolClientId });
    new cdk.CfnOutput(this, "Issuer", { value: issuer });
  }
}
