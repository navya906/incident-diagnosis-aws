"""Real-capture tooling: written for the user to run later, tested here only in dry-run form."""

import sys
from pathlib import Path

import pytest
import yaml

from app.contracts.taxonomy import FAULT_TYPES
from app.offline import fault_injection as fi

TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "test-stack" / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node)}
    return {suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template():
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.mark.parametrize("fault", [f.value for f in FAULT_TYPES])
@pytest.mark.parametrize("revert", [False, True])
def test_every_fault_has_inject_and_revert_plan(fault, revert):
    steps = fi.plan(fault, revert)
    assert steps and all(s.description and callable(s.action) for s in steps)


def test_unknown_fault_rejected():
    with pytest.raises(ValueError):
        fi.plan("gremlins")


def test_dry_run_is_default_and_never_imports_boto3(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # any import attempt would fail
    assert fi.main(["--stack", "s", "--fault", "security_group_misconfiguration"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "revoke tcp/5432" in out


def test_stack_refs_require_all_outputs():
    with pytest.raises(ValueError, match="missing"):
        fi.StackRefs.from_outputs({"AlbUrl": "x"})
    refs = fi.StackRefs.placeholder()
    assert fi.StackRefs.from_outputs(vars(refs)) == refs


def test_template_outputs_match_tool_expectations(template):
    outputs = set(template["Outputs"])
    assert {f for f in vars(fi.StackRefs.placeholder())} <= outputs


def test_template_has_reference_topology(template):
    types = {r["Type"] for r in template["Resources"].values()}
    assert {
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ECS::Service",
        "AWS::RDS::DBInstance",
        "AWS::SQS::Queue",
        "AWS::Lambda::Function",
        "AWS::EC2::SecurityGroup",
        "AWS::IAM::ManagedPolicy",
    } <= types
    assert template["Resources"]["Database"]["Properties"]["ManageMasterUserPassword"] is True
