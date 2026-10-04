"""Customer-facing text templates: simple Hinglish, no LLM, under 160 chars.

One function per message so the pipeline (and later the agent) never
free-styles words to the customer. Keep every string short enough for
a chat bubble and for cheap SMS-style reading.
"""

from __future__ import annotations

MAX_LEN = 160  # every template must fit in a chat bubble


def _fit(text: str) -> str:
    """Hard cap: truncate, never overflow the bubble."""
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"


def listening() -> str:
    """Acknowledgement while the voice note is being transcribed."""
    return "Sun raha hoon… 1 minute rukiye."


def clarify_item(raw: str, candidates: list[str]) -> str:
    """Ambiguous item: offer numbered candidates from the catalog."""
    options = " / ".join(f"{i + 1}) {c}" for i, c in enumerate(candidates[:3]))
    return _fit(f"'{raw}' samajh nahi aaya. Kaunsi wali: {options}? Number likhiye.")


def clarify_unit(item: str) -> str:
    """Unit said does not match the item's selling unit."""
    return f"{item} kis hisaab se? kg, litre ya packet me likhiye."


def confirm_unusual_qty(item: str, qty: float, usual: float) -> str:
    """Quantity far above the customer's usual basket: confirm explicitly."""
    return _fit(f"{qty:g} {item}? Aap usually {usual:g} lete hain."
                " Confirm hai to HAAN likhiye, warna sahi wazan likhiye.")


def clarify_qty(item: str) -> str:
    """Item known, weight missing: ask for it plainly."""
    return f"{item} kitna? Wazan likhiye, jaise 2 kilo."


def order_cancelled() -> str:
    """Customer called the order off after a clarification question."""
    return "Theek hai, order cancel kar diya. Kuch aur chahiye to bataiye."


def awaiting_owner() -> str:
    """Order needs the owner's credit approval."""
    return "Udhaar approval ke liye malik ko bheja hai. Thodi der me batata hoon."


def owner_declined() -> str:
    """Owner said no to credit: polite, offer cash/UPI."""
    return "Maaf kijiye, malik ne udhaar approval nahi diya. Cash/UPI par bhej dun?"


def short_stock(item: str, available: float) -> str:
    """Not enough on the shelf: state what exists, offer it."""
    return f"{item} sirf {available:g} bacha hai. Utna bhej dun? HAAN ya NA likhiye."


def bill_intro() -> str:
    """Default bill header (LLM text only ever replaces this if numbers match)."""
    return "Aapka bill ready hai:"


def delay_nudge_staff(order_id: int, minutes: int) -> str:
    """L1: poke the assigned staffer (staff screens may show order ids)."""
    return f"Order #{order_id} ko {minutes} min ho gaye, status update karein."


def delay_owner_alert(order_id: int, reason: str, reassigned_to: str) -> str:
    """L2: tell the owner why, and who now has the job."""
    return _fit(f"Order #{order_id} late hai: {reason}"
                f" {reassigned_to} ko de diya hai.")


def status_packing_started() -> str:
    """Order confirmed and handed to the packer."""
    return "Order pack ho raha hai. Taiyaar hote hi nikal jayega."


def status_out_for_delivery(eta: str) -> str:
    """Rider left the shop."""
    return f"Order nikal gaya hai! {eta} me pahunch jayega."


def status_delivered(mode: str) -> str:
    """Delivered + how it was paid."""
    return f"Order pahunch gaya! {mode} me payment liya. Dhanyavaad!"


def delivery_problem() -> str:
    """Delivery failed, retry queued."""
    return "Delivery me dikkat aayi, dobara bhej rahe hain. Thoda intezaar kijiye."


def delay_customer(eta_text: str, link: str) -> str:
    """L2: customer delay note with tracking link. No ids, phones or balances."""
    return _fit(f"Delivery me deri ho rahi hai, {eta_text} pahunch jayega."
                f" Track: {link}")


def could_not_understand() -> str:
    """STT/LLM failed or nothing extractable: ask for a clean resend."""
    return "Samajh nahi aaya. Item, wazan aur naap likhkar dobara bhejie."
