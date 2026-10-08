"""Live-data local validation only; no Gmail calls."""
from pathlib import Path
from ralfloop_agent.unified_assistant.email_pipeline import GenericEmailPipeline
from ralfloop_agent.unified_assistant.email import EmailWorkingMemoryBuilder
from ralfloop_agent.unified_assistant.memory import MemoryRouter
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade

names = ("PiGreco.txt", "Amici_Parco_Trotter.txt", "AMEDI.txt")
docs = Path(__file__).resolve().parents[1] / "docs" / "perrifarci" / "email_drafts"
registry=UnifiedRegistryFacade()
builder=EmailWorkingMemoryBuilder(MemoryRouter(),registry.domain("email"))
for name in names:
    txt=(docs/name).read_text()
    body=txt.strip()
    ctx=builder.build(objective="Chiedi scusa per gli errori sulla lettera di sostegno PerRifArci e domanda la firma del nuovo Allegato A2 con cofinanziamento € 0,00",recipient=name,subject="PerRifArci: rettifica Allegato A2")
    result=GenericEmailPipeline().validate_exact(ctx,body)
    print(name,result.hard_guard,result.final_validator,result.risk,len(body))
    assert result.hard_guard == "passed" and result.final_validator == "passed",name
