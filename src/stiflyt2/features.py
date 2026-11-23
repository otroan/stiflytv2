"""The features of the tool: the parts a person may be given the use of, one by one.

A feature is a part that stands by itself: its own module here, its own
endpoints and assistant tools, its own part of a page, its own tables where it
keeps something. It names itself here, and everything that belongs to it asks
for it by that name (`auth.may("signs")`). Who has it is said in the database
(`access.py`): a role is a bundle of features, a person may be given one more
or have one taken away, and a feature as a whole is off, for those it is given
to, or for everybody who is signed in.

Looking is not a feature: whoever is let in may look at the routes and the
signs. A part that not everybody may even look at (who owns the ground) is a
feature for the looking too.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

STATES = ("off", "given", "all")      # for nobody; for those whose role or own list has it; for everybody let in


@dataclass(frozen=True)
class Feature:
    key: str
    title: str          # what it lets one do, as said to a person
    ask: str            # what one who lacks it is told to ask for
    state: str = "given"        # how it stands until the database says otherwise


FEATURES: dict[str, Feature] = {}


def feature(key: str, title: str, ask: str, state: str = "given") -> Feature:
    """Name a feature. Called once by the module that is the feature, when it is loaded."""
    if state not in STATES:
        raise ValueError(f"a feature is one of: {', '.join(STATES)}")
    FEATURES[key] = Feature(key, title, ask, state)
    return FEATURES[key]


# The modules that are features. Each names itself when it is loaded; listed here so that whoever asks which
# features there are (the command line, which loads none of them for its own sake) finds them all.
MODULES = ("stiflyt2.ops.trips", "stiflyt2.presence", "stiflyt2.ops.issues", "stiflyt2.ops.photos", "stiflyt2.ops.reports",
           "stiflyt2.ops.landowners")


def load() -> dict[str, Feature]:
    """Every feature there is, with the modules that are features loaded so that each has named itself."""
    for name in MODULES:
        importlib.import_module(name)
    return FEATURES


# The two there were before features had names. The roles that had them are bundles of them now.
feature("routes", "change the routes, the places and the lines", "ask for the editor role")
feature("signs", "decide about signs and take out orders", "ask for the operations role")
