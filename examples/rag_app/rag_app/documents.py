"""Synthetic policy documents for a fictional retailer, "Northwind Outfitters".

This data is entirely made up for this example app. It is not real company
policy and should never be presented as such.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    text: str


DOCUMENTS: list[Document] = [
    Document(
        id="policy-returns-001",
        title="Returns and Refunds Policy",
        text=(
            "Northwind Outfitters accepts returns within 45 days of purchase for a full refund "
            "to the original payment method. Items must be unworn, unwashed, and include the "
            "original tags. Sale items marked final sale are not eligible for return. Refunds "
            "are processed within 5 to 7 business days after we receive the returned item."
        ),
    ),
    Document(
        id="policy-shipping-002",
        title="Shipping Policy",
        text=(
            "Standard shipping within the continental United States takes 5 to 8 business days "
            "and is free on orders over 75 dollars. Expedited shipping is available at checkout "
            "for an additional fee and arrives in 2 to 3 business days. Orders are processed "
            "within 1 business day of purchase, excluding weekends and holidays."
        ),
    ),
    Document(
        id="policy-warranty-003",
        title="Product Warranty",
        text=(
            "All Northwind Outfitters hiking boots and outerwear carry a 2 year manufacturer "
            "warranty against defects in materials and workmanship. The warranty does not cover "
            "normal wear and tear, accidental damage, or improper care. To file a warranty claim, "
            "contact customer support with your order number and photos of the defect."
        ),
    ),
    Document(
        id="policy-giftcards-004",
        title="Gift Card Terms",
        text=(
            "Gift cards never expire and can be used toward any purchase on our website or in "
            "store. Gift cards cannot be redeemed for cash except where required by law. Lost or "
            "stolen gift cards cannot be replaced without the original purchase receipt."
        ),
    ),
    Document(
        id="policy-international-005",
        title="International Shipping",
        text=(
            "We ship to over 30 countries outside the United States. International orders may be "
            "subject to customs duties and import taxes that are the responsibility of the "
            "customer. International shipping typically takes 10 to 21 business days depending on "
            "destination and customs processing."
        ),
    ),
    Document(
        id="policy-loyalty-006",
        title="Loyalty Rewards Program",
        text=(
            "Members of the Northwind Trailblazer rewards program earn 1 point for every dollar "
            "spent. Points can be redeemed for discounts on future purchases at a rate of 100 "
            "points for 5 dollars. Members receive a birthday reward and early access to seasonal "
            "sales."
        ),
    ),
    Document(
        id="policy-pricematch-007",
        title="Price Matching",
        text=(
            "Northwind Outfitters will match the price of an identical in-stock item found at a "
            "major competitor within 14 days of purchase. Price match requests must include a "
            "link or advertisement showing the lower price. Clearance and flash sale prices are "
            "excluded from price matching."
        ),
    ),
    Document(
        id="policy-cancellation-008",
        title="Order Cancellation",
        text=(
            "Orders can be cancelled free of charge within 1 hour of placing the order by "
            "contacting customer support. Once an order has entered the fulfillment process it "
            "cannot be cancelled, but it can be returned after delivery according to the Returns "
            "and Refunds Policy."
        ),
    ),
]

DOCUMENTS_BY_ID: dict[str, Document] = {doc.id: doc for doc in DOCUMENTS}
