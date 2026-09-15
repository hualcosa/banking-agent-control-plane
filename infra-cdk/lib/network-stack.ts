import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

/** VPC the AgentCore Runtime (VPC mode), RDS and the endpoints it needs live in. */
export class NetworkStack extends cdk.Stack {
  readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, "Vpc", {
      // Listed explicitly so `cdk synth` never needs an account lookup.
      availabilityZones: ["sa-east-1a", "sa-east-1b", "sa-east-1c"],
      // ponytail: one NAT is a single point of failure; natGateways: 3 when the SLO asks for it.
      natGateways: 1,
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: "private", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    // Image pulls and log shipping stay inside the VPC instead of going out the NAT.
    this.vpc.addInterfaceEndpoint("EcrDocker", { service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER });
    this.vpc.addInterfaceEndpoint("EcrApi", { service: ec2.InterfaceVpcEndpointAwsService.ECR });
    this.vpc.addInterfaceEndpoint("Logs", { service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS });
  }
}
