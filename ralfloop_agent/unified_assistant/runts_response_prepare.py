"""Trusted local Suite binding. Only a practice ID is supplied by the caller."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .contracts import StrictModel
from .memory_service import MemoryEntity
from .runts_modeld_plan import ModelDPlan


class RuntsPrepareBinding(StrictModel):
    suite: str
    output: str
    decision_plan: str
    official_model: str
    golden_pdf: str
    golden_sha256: str
    authoritative_message_hash: str


class RuntsSuiteResponsePreparer:
    def __init__(self, binding: RuntsPrepareBinding):
        self.binding=binding

    def __call__(self, practice, messages, memory):
        b=self.binding
        plan=ModelDPlan.model_validate_json(Path(b.decision_plan).read_text())
        if practice.native_id!=plan.practice_id:raise ValueError("practice_binding_mismatch")
        authoritative=[m for m in messages if m.native_id==plan.message_id and m.practice_id==practice.native_id]
        if len(authoritative)!=1 or authoritative[0].source.content_hash!=b.authoritative_message_hash:
            raise ValueError("STALE_PROPOSAL")
        message=authoritative[0]
        if any(m.native_id!=message.native_id and (m.published_at is None or message.published_at is None or m.published_at>=message.published_at) for m in messages):
            raise ValueError("STALE_PROPOSAL")
        plan.persist(memory)
        # Consume the structured persisted decision versions, not a prompt blob.
        reloaded=[]
        for fact in plan.facts:
            entity=memory.get_entity(fact.storage_id)
            reloaded.append(type(fact).model_validate({k:entity.data[k] for k in type(fact).model_fields}))
        plan=plan.model_copy(update={"facts":tuple(reloaded)})
        plan.validate_scope()
        totals=plan.fact("APPROVED_TOTALS","balance").value

        # The binding output is a trusted private base directory.
        # Every PREPARE gets its own 0700 directory so an execution
        # never overwrites artifacts created by another OS user/run.
        output_base = Path(b.output)
        output_base.mkdir(parents=True, exist_ok=True)
        run_output = Path(
            tempfile.mkdtemp(
                prefix=f"prepare-{practice.native_id}-",
                dir=str(output_base),
            )
        )

        args=["--suite",b.suite,"--output",str(run_output),"--year",str(plan.exercise),"--practice-id",plan.practice_id,
              "--message-id",plan.message_id,"--message-hash",b.authoritative_message_hash,"--official-model",b.official_model,
              "--approved-income",totals["income"],"--approved-expense",totals["expense"],"--approved-surplus",totals["surplus"],
              "--approved-closing",totals["deposits"],"--decision-plan",b.decision_plan,"--golden-pdf",b.golden_pdf,"--golden-sha256",b.golden_sha256]
        root = Path(__file__).resolve().parents[2]
        suite_python = Path(b.suite) / ".venv/bin/python"
        script = root / "scripts/runts_modd_prepare.py"
        proposal_path = run_output / "proposal.json"

        if not suite_python.is_file():
            raise ValueError("runts_suite_python_missing")
        if not script.is_file():
            raise ValueError("runts_prepare_script_missing")

        # runts_modd_prepare needs both dependency sets:
        # - RUNTS Suite venv: SQLAlchemy, WeasyPrint, Suite application
        # - Ralfloop backend: PyYAML, Pydantic and Ralfloop dependencies
        #
        # Execute with the Suite interpreter and expose only the existing
        # Ralfloop site-packages through PYTHONPATH. No package installation
        # or mutation of either virtualenv is required.
        site_paths = []
        for candidate in sys.path:
            if (
                candidate
                and "site-packages" in candidate
                and Path(candidate).is_dir()
                and candidate not in site_paths
            ):
                site_paths.append(candidate)

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"

        python_path = [str(root), *site_paths]
        inherited_python_path = env.get("PYTHONPATH")
        if inherited_python_path:
            python_path.extend(
                p
                for p in inherited_python_path.split(os.pathsep)
                if p and p not in python_path
            )

        env["PYTHONPATH"] = os.pathsep.join(python_path)

        result = subprocess.run(
            [str(suite_python), str(script), *args],
            cwd="/tmp",
            env=env,
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "no subprocess output"
            )
            raise ValueError(
                "runts_prepare_subprocess_failed: "
                + detail[-4000:]
            )

        if not proposal_path.is_file():
            raise ValueError("runts_prepare_proposal_missing")

        from .runts_document_prepare import DocumentReviewProposal

        proposal = DocumentReviewProposal.model_validate_json(
            proposal_path.read_text()
        )

        memory.put_entity(MemoryEntity.build(entity_id=proposal.proposal_id,domain="runts",entity_type="RUNTS_DOCUMENT_PROPOSAL",
            status=proposal.status,updated_at=datetime.now(timezone.utc),data=proposal.model_dump(mode="json"),provenance=proposal.provenance))
        return proposal
