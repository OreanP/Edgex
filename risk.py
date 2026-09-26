"""Moteur de risque déterministe (partie D).

Principe : le LLM propose, ce code borne. Le broker appelle check() avant
CHAQUE ordre : aucun trade ne passe sans lui, même si l'agent « oublie »
de consulter le risque.

Taille des positions : fraction de Kelly, plafonnée.
Pour une part qui coûte a et rapporte 1 $ si l'événement se produit, avec une
probabilité p, la fraction de Kelly vaut f* = (p - a) / (1 - a).
Hypothèse de la formule : p est la VRAIE probabilité. Ici p est une estimation
bruitée du LLM, d'où la fraction (1/4), le multiplicateur de confiance et les plafonds.
"""

from __future__ import annotations

from .schemas import Forecast, OrderRequest, Portfolio, Quote, RiskConfig, RiskDecision, Side


def edge_for(side: Side, probability: float, quote: Quote) -> tuple[float, float]:
    """Renvoie (edge, prix payé). L'edge se mesure contre le prix d'achat réel (ask),
    pas contre le prix affiché : sinon la fourchette mange l'avantage."""
    price = quote.ask_for(side)
    p_side = probability if side == "YES" else 1.0 - probability
    return p_side - price, price


def max_position(
    side: Side,
    forecast: Forecast,
    quote: Quote,
    portfolio: Portfolio,
    cfg: RiskConfig | None = None,
    requested_usd: float = 0.0,
) -> RiskDecision:
    """Montant maximal autorisé sur ce côté. Sert aussi d'outil « evaluate_risk » pour l'agent."""
    cfg = cfg or RiskConfig()
    edge, price = edge_for(side, forecast.probability, quote)
    reasons: list[str] = []

    def refuse(reason: str) -> RiskDecision:
        return RiskDecision(
            approved=False,
            requested_usd=requested_usd,
            max_amount_usd=0.0,
            edge=round(edge, 4),
            price=price,
            reasons=reasons + [reason],
        )

    multiplier = cfg.confidence_multiplier.get(forecast.confidence, 0.0)
    if multiplier <= 0:
        return refuse(f"Confiance « {forecast.confidence} » : aucune position autorisée.")
    if quote.spread > cfg.max_spread:
        return refuse(f"Fourchette trop large ({quote.spread:.2f} > {cfg.max_spread:.2f}) : marché peu liquide.")
    if price <= 0.0 or price >= 1.0:
        return refuse(f"Prix d'achat invalide ({price:.2f}).")
    if edge < cfg.min_edge:
        return refuse(f"Edge de {edge:+.2f} sous le seuil de {cfg.min_edge:.2f}.")

    bankroll = portfolio.bankroll_usd
    kelly_full = edge / (1.0 - price)
    kelly_usd = cfg.kelly_fraction * kelly_full * multiplier * bankroll
    cap_usd = cfg.max_position_pct * bankroll
    room_usd = max(0.0, cfg.max_total_exposure_pct * bankroll - portfolio.exposure_usd)
    max_usd = round(min(kelly_usd, cap_usd, room_usd, portfolio.cash_usd), 2)
    reasons.append(
        f"Kelly {kelly_full:.1%} × {cfg.kelly_fraction} × confiance {multiplier} = {kelly_usd:.2f} $ ; "
        f"plafond par position {cap_usd:.2f} $ ; marge d'exposition {room_usd:.2f} $ ; "
        f"cash {portfolio.cash_usd:.2f} $ → maximum {max_usd:.2f} $."
    )
    if max_usd < cfg.min_trade_usd:
        return refuse(f"Maximum autorisé ({max_usd:.2f} $) sous le minimum de {cfg.min_trade_usd:.2f} $.")
    return RiskDecision(
        approved=True,
        requested_usd=requested_usd,
        max_amount_usd=max_usd,
        edge=round(edge, 4),
        price=price,
        reasons=reasons,
    )


def check(
    order: OrderRequest,
    forecast: Forecast,
    quote: Quote,
    portfolio: Portfolio,
    cfg: RiskConfig | None = None,
) -> RiskDecision:
    """Valide un ordre précis. S'il dépasse le maximum, il est refusé MAIS le maximum est
    renvoyé : l'agent peut réessayer avec un montant autorisé (moment clé de la démo)."""
    if order.market_id != quote.market_id:
        raise ValueError("L'ordre et la cotation ne portent pas sur le même marché.")
    limit = max_position(order.side, forecast, quote, portfolio, cfg, requested_usd=order.amount_usd)
    if not limit.approved:
        return limit
    if order.amount_usd > limit.max_amount_usd + 1e-9:
        return limit.model_copy(
            update={
                "approved": False,
                "reasons": limit.reasons
                + [
                    f"Demandé {order.amount_usd:.2f} $ > maximum {limit.max_amount_usd:.2f} $ : "
                    "réessayer avec au plus ce montant."
                ],
            }
        )
    return limit.model_copy(update={"reasons": limit.reasons + [f"Ordre de {order.amount_usd:.2f} $ accepté."]})
