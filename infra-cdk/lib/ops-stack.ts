import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

export interface OpsStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  dbSecurityGroup: ec2.ISecurityGroup;
}

/** Where the operator stands at 3am: a private instance reachable only through SSM, with a path to Postgres. */
export class OpsStack extends cdk.Stack {
  readonly bastion: ec2.Instance;

  constructor(scope: Construct, id: string, props: OpsStackProps) {
    super(scope, id, props);

    const sg = new ec2.SecurityGroup(this, "BastionSg", { vpc: props.vpc, allowAllOutbound: true });
    props.dbSecurityGroup.addIngressRule(sg, ec2.Port.tcp(5432), "operator via SSM port forward");

    this.bastion = new ec2.Instance(this, "Bastion", {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.NANO),
      machineImage: ec2.MachineImage.latestAmazonLinux2023({ cpuType: ec2.AmazonLinuxCpuType.ARM_64 }),
      securityGroup: sg,
      ssmSessionPermissions: true,
    });

    new cdk.CfnOutput(this, "BastionInstanceId", { value: this.bastion.instanceId });
  }
}
