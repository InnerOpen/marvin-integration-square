"""Workflow Library recipes Square ships (SDK 0.9.0): offered in Marvin once a Square connection exists.

``reset-square-listings`` is the go-live and clean-up button for the loop ``content.py`` declares. The list-for-sale
workflow lists an item once per ``price|shipping|pickup`` and records that in ``square_listed_for``; this overwrites it
with a value no price can equal, so the workflow lists every item again. It is also how items listed before studio
pickup existed get their pickup link.
"""

from marvin_integration_sdk import WorkflowRecipe

# The metadata key square-list-for-sale compares against "<price>|<shipping fee>|<pickup>" (content.LISTED_FOR). Any value
# without a "|" never matches, so the item is listed again; "reset" says why when someone reads the entry's metadata.
LISTED_FOR_KEY = "square_listed_for"
RESET_VALUE = "reset"

RESET_LISTINGS = WorkflowRecipe(
    id="reset-square-listings",
    title="Re-list every item for sale on Square",
    outcome=(
        "Run by hand, it lists every published item with Sell online on (and not yet sold) on Square again: its catalog item "
        "refreshed (a new one only if Square no longer has it), a stock of 1 and new checkout links (shipped, and studio pickup "
        "where it is offered) saved on the entry."
    ),
    category="Square",
    tags=("commerce", "go-live"),
    definition={
        "trigger": {"type": "manual"},
        "target": {
            "entity": "entry",
            "query": {
                "entry_type": "{{entry_type}}",
                "status": "published",
                "where": [
                    {"field": "sellOnline", "op": "eq", "value": True},
                    {"field": "status", "op": "neq", "value": "sold"},
                ],
            },
        },
        "actions": [{"kind": "entry", "op": "set_metadata", "metadata": {LISTED_FOR_KEY: RESET_VALUE}}],
    },
    variables={
        "entry_type": {
            "type": "entry_type_slug",
            "description": "The type whose entries are for sale (the one the Square shop fields were added to).",
            "example": "artwork",
        },
    },
    side_effects=(
        {"kind": "content_mutation", "description": "Marks every matching entry for relisting."},
        {
            "kind": "external_post",
            "description": "The 'Square: list for sale' workflow then creates or refreshes each item's Square listing (it must be switched on).",
        },
    ),
)

RECIPES = (RESET_LISTINGS,)
