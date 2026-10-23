"""Explicit accounting basis for model contribution, separate from sales prices."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PricingBasis:
    price_vat_basis: str = "unknown"
    cost_vat_basis: str = "unknown"
    commission_basis: str = "unknown"

    def __post_init__(self):
        for value in (self.price_vat_basis, self.cost_vat_basis):
            if value not in {"unknown", "ex_vat", "incl_25"}:
                raise ValueError("Momsgrundlag skal være unknown, ex_vat eller incl_25")
        if self.commission_basis not in {"unknown", "guest_price", "ex_vat"}:
            raise ValueError("Provisionsgrundlag skal være unknown, guest_price eller ex_vat")

    @property
    def confirmed(self):
        return "unknown" not in (self.price_vat_basis, self.cost_vat_basis, self.commission_basis)

    def cost_ex_vat(self, cost):
        return cost / 1.25 if self.cost_vat_basis == "incl_25" else cost

    def proceeds(self, price, commission):
        if not self.confirmed:
            return price * (1 - commission)
        revenue = price / 1.25 if self.price_vat_basis == "incl_25" else price
        fee_base = price if self.commission_basis == "guest_price" else revenue
        return revenue - fee_base * commission

    def contribution(self, price, commission, cost):
        # Preserve the explicitly labelled legacy estimate until all bases are
        # confirmed. Partial conversion would silently mix accounting units.
        cost = self.cost_ex_vat(cost) if self.confirmed else cost
        return self.proceeds(price, commission) - cost

    def price_for_contribution(self, contribution, commission, cost):
        cost = self.cost_ex_vat(cost) if self.confirmed else cost
        coefficient = self.proceeds(1.0, commission)
        if coefficient <= 0:
            raise ValueError("Provisionen efterlader intet positivt prisbidrag")
        return (contribution + cost) / coefficient
