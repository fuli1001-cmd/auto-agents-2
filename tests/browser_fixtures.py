"""Complete archived visual fixtures for workflow tests that mock rendering."""
import base64

from auto_agents_2.design import prototype_routes

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=")


def prototype_rows(directory, actions, artifacts):
    image = artifacts.put(PNG, "screenshot")
    return [{"route": route, "resolved_route": resolved, "viewport": width, "status": 200, "errors": [],
             "actions": [action.model_dump() for action in actions if action.route == route], "image_artifact": image}
            for route, resolved in prototype_routes(directory, actions) for width in (1440, 390)]
