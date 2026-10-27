"""
Costanti: categorie spese, tipi entrate, emoji
"""

CATEGORIE_SPESA = {
    "🍕 Ristoranti":          "Ristoranti",
    "🚗 Trasporti":     "trasporti",
    "🏠 Spesa":          "spesa",
    "👕 Abbigliamento": "abbigliamento",
    "🎬 Svago":         "svago",
    "💻 Tech":          "tech",
    "💈 Cura persona":  "cura_persona",
    "🎁 Regali":        "regali",
    "✈️ Viaggi":        "viaggi",
    "🏋️ Sport":         "sport",
    "📱 Abbonamenti":   "abbonamenti",
    "❓ Altro":          "altro",
}

TIPI_ENTRATA = {
    "💼 Stipendio":    "stipendio",
    "🎁 Regalo":       "regalo",
    "📈 Investimenti": "investimenti",
    "🏦 Rimborso":     "rimborso",
    "🍀 Mancia":       "mancia",
    "❓ Altro":         "altro_entrata",
}

EMOJI_CATEGORIA = {v: k.split(" ")[0] for k, v in CATEGORIE_SPESA.items()}
EMOJI_ENTRATA   = {v: k.split(" ")[0] for k, v in TIPI_ENTRATA.items()}

CATEGORIA_LABEL = {v: " ".join(k.split(" ")[1:]) for k, v in CATEGORIE_SPESA.items()}
ENTRATA_LABEL   = {v: " ".join(k.split(" ")[1:]) for k, v in TIPI_ENTRATA.items()}


def fmt_amount(amount: float, currency: str = "€") -> str:
    return f"{currency}{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def categoria_display(cat: str) -> str:
    emoji = EMOJI_CATEGORIA.get(cat, "❓")
    label = CATEGORIA_LABEL.get(cat, cat.replace("_", " ").title())
    return f"{emoji} {label}"


def entrata_display(tipo: str) -> str:
    emoji = EMOJI_ENTRATA.get(tipo, "💸")
    label = ENTRATA_LABEL.get(tipo, tipo.replace("_", " ").title())
    return f"{emoji} {label}"
