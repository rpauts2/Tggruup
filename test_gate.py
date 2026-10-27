import sys
sys.path.insert(0, r"C:\Users\Administrator\Desktop\ai\Tggruup")
from tgfarm.config import get_config
from tgfarm.core.schemas import DraftPost, Claim
from tgfarm.s5_quality import gate_length, gate_structure, gate_copyright, gate_facts

cfg = get_config()
persona = cfg.persona("zen_misc_ru")
draft = DraftPost(
    hook="Жительница Ачинска отсудила почти 400 тысяч рублей за некачественный ремонт - Телеканал Енисей.",
    body="Жительница Ачинска отсудила почти 400 тысяч рублей за некачественный ремонт - Телеканал Енисей.\n\nПодробности и первоисточники — в ссылках ниже.",
    sources=[{"title": "Жительница Ачинска...", "url": "https://example.com", "source": "s"}],
    claims=[Claim(claim="x", source_url="https://example.com", verified=True)],
    generation_provider="offline",
)
for g in (gate_length, gate_structure, gate_copyright, gate_facts):
    print(g.__name__, g(draft, persona))