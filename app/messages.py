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


def _with_tracking(text: str, tracking_url: str = "") -> str:
    """Keep the complete tracking URL attached to a short status message."""
    if not tracking_url:
        return _fit(text)
    return f"{_fit(text)} Track: {tracking_url}"


def listening() -> str:
    """Acknowledgement while the voice note is being transcribed."""
    return "Sun raha hoon… 1 minute rukiye."


def clarify_item(raw: str, candidates: list[str]) -> str:
    """Ambiguous item: offer numbered candidates from the catalog."""
    options = " / ".join(f"{i + 1}) {c}" for i, c in enumerate(candidates[:3]))
    return _fit(f"I couldn't match '{raw}'. Which item did you mean: {options}?")


def clarify_unit(item: str) -> str:
    """Unit said does not match the item's selling unit."""
    return f"What unit should I use for {item} (kg, litre, or pack)?"


def confirm_unusual_qty(item: str, qty: float, usual: float) -> str:
    """Quantity far above the customer's usual basket: confirm explicitly."""
    return _fit(f"Confirm {qty:g} {item}? You usually order {usual:g}."
                " Reply YES to confirm or send the correct quantity.")


def clarify_qty(item: str) -> str:
    """Item known, weight missing: ask for it plainly."""
    return f"How much {item} would you like? For example: 2 kg."


def order_cancelled() -> str:
    """Customer called the order off after a clarification question."""
    return "Okay, the order is cancelled. Message us if you need anything else."


def awaiting_owner(tracking_url: str = "") -> str:
    """Order needs the owner's credit approval."""
    return _with_tracking(
        "This order needs shop approval. We'll update you shortly.",
        tracking_url)


def owner_declined() -> str:
    """Owner said no to credit: polite, offer cash/UPI."""
    return "Shop credit wasn't approved. Would you like to pay by cash or UPI?"


def short_stock(item: str, available: float) -> str:
    """Not enough on the shelf: state what exists, offer it."""
    return (f"Only {available:g} {item} available. Would you like that amount?"
            " Reply YES or NO.")


def bill_intro() -> str:
    """Default bill header (LLM text only ever replaces this if numbers match)."""
    return "Your order summary:"


def delay_nudge_staff(order_id: int, minutes: int) -> str:
    """L1: poke the assigned staffer (staff screens may show order ids)."""
    return f"Order #{order_id} ko {minutes} min ho gaye, status update karein."


def delay_owner_alert(order_id: int, reason: str, reassigned_to: str) -> str:
    """L2: tell the owner why, and who now has the job."""
    return _fit(f"Order #{order_id} late hai: {reason}"
                f" {reassigned_to} ko de diya hai.")


def status_packing_started(tracking_url: str = "") -> str:
    """Order confirmed and handed to the packer."""
    return _with_tracking("Your order is being packed.", tracking_url)


def status_out_for_delivery(eta: str, tracking_url: str = "") -> str:
    """Rider left the shop."""
    return _with_tracking(f"Your order is on the way. ETA: {eta}.", tracking_url)


def status_delivered(mode: str, tracking_url: str = "") -> str:
    """Delivered + how it was paid."""
    return _with_tracking(f"Delivered. Payment: {mode}. Thank you!", tracking_url)


def delivery_problem(tracking_url: str = "") -> str:
    """Delivery failed, retry queued."""
    return _with_tracking("Delivery issue — we're arranging another attempt.",
                          tracking_url)


def delay_customer(eta_text: str, link: str) -> str:
    """L2: customer delay note with tracking link. No ids, phones or balances."""
    return _with_tracking(f"Your order is delayed. Updated ETA: {eta_text}.", link)


def could_not_understand() -> str:
    """STT/LLM failed or nothing extractable: ask for a clean resend."""
    return ("I couldn't read that order. Try: “2 kg sugar and 1 kg atta.”")
