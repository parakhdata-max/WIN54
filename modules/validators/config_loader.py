import json
import os


def load_validation_config():

    base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    path = os.path.join(base, "config", "validation_rules.json")

    with open(path, "r") as f:
        return json.load(f)
