"""The two IAM grants the daily BigQuery export cannot run without.

`roles/bigquery.dataEditor` permits writing rows; it does NOT permit creating
the load job that writes them. The project-level `roles/bigquery.jobUser` was
missing, so the daily export had never once succeeded — and the job endpoint
reported the failure as HTTP 200 "partial", so nothing surfaced until somebody
counted the rows. See docs/25.

These assertions read the Terraform source rather than the live project,
following `test_bq_schema_parity.py`. They cannot prove the grants exist in
GCP — only a project owner can read or set that policy. What they do prove is
that both are declared, so the requirement is configuration rather than a
comment someone has to notice, and that they are declared in the ONE form that
does not take the client's own access away.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BIGQUERY_TF = Path("infra/terraform/bigquery.tf")
VARIABLES_TF = Path("infra/terraform/variables.tf")


@pytest.fixture(scope="module")
def bigquery_tf() -> str:
    return BIGQUERY_TF.read_text(encoding="utf-8")


def test_both_grants_the_export_depends_on_are_declared(bigquery_tf: str) -> None:
    assert 'role       = "roles/bigquery.dataEditor"' in bigquery_tf
    assert 'role    = "roles/bigquery.jobUser"' in bigquery_tf


def test_the_grants_are_additive_members_not_authoritative_bindings(bigquery_tf: str) -> None:
    """The footgun, and the reason this test is worth more than the one above.

    `google_bigquery_dataset_iam_binding` is authoritative for its role and
    `_policy` for the whole dataset. Either would delete the client's own access
    to a dataset THEY created and still use, silently, on the first apply. The
    `_member` forms add one grant and leave every other binding alone.
    """
    offenders = re.findall(r'resource "(google_\w*iam_(?:binding|policy))"', bigquery_tf)
    assert not offenders, (
        "authoritative IAM resources remove every member not listed in them; "
        f"use the _member form instead: {offenders}"
    )
    assert 'resource "google_bigquery_dataset_iam_member"' in bigquery_tf
    assert 'resource "google_project_iam_member"' in bigquery_tf


def test_the_grants_are_gated_and_default_to_off(bigquery_tf: str) -> None:
    """Ungated, these would fail every apply the developer runs.

    The developer account holds Editor, which lacks
    `resourcemanager.projects.setIamPolicy` — which is why every other IAM
    binding in this stack is a documented owner-run command. An ungated
    resource would break the image-pin apply that follows each deploy.
    """
    assert bigquery_tf.count("count = var.manage_bigquery_iam ? 1 : 0") == 2

    variables = VARIABLES_TF.read_text(encoding="utf-8")
    declaration = variables.split('variable "manage_bigquery_iam"', 1)
    assert len(declaration) == 2, "manage_bigquery_iam is not declared"
    body = declaration[1].split("}", 1)[0]
    assert "default     = false" in body


def test_the_manual_commands_stay_documented(bigquery_tf: str) -> None:
    """The gate defaults to off, so the owner-run commands are still the live
    route. Deleting them in favour of the resources would leave the person who
    can actually grant these with nothing to run."""
    assert "add-iam-policy-binding" in bigquery_tf
    assert "roles/bigquery.jobUser" in bigquery_tf
