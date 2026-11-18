def audit(policy):
    actions = policy.get_allowed_actions()
    permissions_on_other_users(policy, actions)


def precise_recheck(policy, role_arns, principal_attributes=None, step_conditions=None):
    """
    Optional precise re-check for the AttachRolePolicy/PutRolePolicy +
    sts:AssumeRole escalation combinations.

    Unlike the default set-based audit above, this evaluates, for each
    concrete role ARN given by the caller, whether the *same* role can be
    both modified and assumed under the supplied principal attributes and
    per-step request condition values.  See parliament.privesc_precheck for
    the exact semantics.  The result only covers the given identity policy
    and context; it says nothing about role trust policies or organization
    policies.
    """
    from ..privesc_precheck import precise_recheck as _precise_recheck

    return _precise_recheck(
        policy, role_arns, principal_attributes, step_conditions
    )


# Categories based on https://know.bishopfox.com/blog/5-privesc-attack-vectors-in-aws


def permissions_on_other_users(policy, expanded_actions):
    # Turn into lowercase
    expanded_actions_normalized = [x.lower() for x in expanded_actions]
    expanded_actions = set(expanded_actions_normalized)

    escalation_methods = {
        # 1. IAM Permissions on Other Users
        "CreateAccessKey": ["iam:createaccesskey"],
        "CreateLoginProfile": ["iam:createloginprofile"],
        "UpdateLoginProfile": ["iam:updateloginprofile"],
        # 2. Permissions on Policies
        "CreateNewPolicyVersion": ["iam:createpolicyversion"],
        "SetExistingDefaultPolicyVersion": ["iam:setdefaultpolicyversion"],
        "AttachUserPolicy": ["iam:attachuserpolicy"],
        "AttachGroupPolicy": ["iam:attachgrouppolicy"],
        "AttachRolePolicy": ["iam:attachrolepolicy", "sts:assumerole"],
        "PutUserPolicy": ["iam:putuserpolicy"],
        "PutGroupPolicy": ["iam:putgrouppolicy"],
        "PutRolePolicy": ["iam:putrolepolicy", "sts:assumerole"],
        "AddUserToGroup": ["iam:addusertogroup"],
        # 3. Updating an AssumeRolePolicy
        "UpdateRolePolicyToAssumeIt": ["iam:updateassumerolepolicy", "sts:assumerole"],
        # 4. iam:PassRole:*
        "CreateEC2WithExistingIP": ["iam:passrole", "ec2:runinstances"],
        "PassExistingRoleToNewLambdaThenInvoke": [
            "iam:passrole",
            "lambda:createfunction",
            "lambda:invokefunction",
        ],
        "PassExistingRoleToNewLambdaThenTriggerWithNewDynamo": [
            "iam:passrole",
            "lambda:createfunction",
            "lambda:createeventsourcemapping",
            "dynamodb:createtable",
            "dynamodb:putitem",
        ],
        "PassExistingRoleToNewLambdaThenTriggerWithExistingDynamo": [
            "iam:passrole",
            "lambda:createfunction",
            "lambda:createeventsourcemapping",
        ],
        "PassExistingRoleToNewGlueDevEndpoint": [
            "iam:passrole",
            "glue:createdevendpoint",
        ],
        "PassExistingRoleToCloudFormation": [
            "iam:passrole",
            "cloudformation:createstack",
        ],
        "PassExistingRoleToNewDataPipeline": [
            "iam:passrole",
            "datapipeline:createpipeline",
        ],
        # 5. Privilege Escalation Using AWS Services
        "UpdateExistingGlueDevEndpoint": ["glue:updatedevendpoint"],
        "EditExistingLambdaFunctionWithRole": ["lambda:updatefunctioncode"],
    }

    for key in escalation_methods:
        if set(escalation_methods[key]).issubset(expanded_actions):
            policy.add_finding(
                "PRIVILEGE_ESCALATION",
                location={"type": key, "actions": escalation_methods[key]},
            )
