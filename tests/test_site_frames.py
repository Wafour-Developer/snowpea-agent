"""Frame action memory schema preserves scope and refuses unsafe frame selectors."""

import pytest
from pydantic import ValidationError

from snowpea_core.server.protocol import CAPABILITIES, SiteEntryInput
from snowpea_core.session.site_memory import SiteMemoryError, check_entry


def payload(frames):
    return {
        "pageType": "editor",
        "urlPattern": "/editor",
        "summary": "Editor",
        "landmarks": [],
        "actions": [
            {
                "name": "title",
                "kind": "fill",
                "frames": frames,
                "locators": [{"by": "role", "role": "textbox", "name": "Article title"}],
            }
        ],
        "flows": [],
        "pitfalls": [],
        "fingerprint": "ax1:abcdef",
    }


@pytest.mark.parametrize(
    "origin", ["https://example.com", "http://127.0.0.1:4321", "http://[::1]:4321"]
)
def test_frame_scope_roundtrip(origin):
    frames = [{"selector": "#editor", "origin": origin, "path": "/editor/*"}]
    data = SiteEntryInput.model_validate(payload(frames)).model_dump()
    check_entry(data)
    assert data["actions"][0]["frames"] == frames
    assert "siteFrameActions" in CAPABILITIES


@pytest.mark.parametrize(
    "change",
    [
        {"selector": "iframe:nth-child(2)"},
        {"selector": "iframe >> text=Pay"},
        {"selector": "iframe[src=secret]"},
        {"origin": "https://example.com/path"},
        {"origin": "file://local"},
        {"path": "/editor?token=secret"},
        {"selector": 'iframe[title="kim@example.org"]'},
        {"path": "/editor/123456789"},
    ],
)
def test_unsafe_frame_scope_refused(change):
    frame = {"selector": "#editor", "origin": "https://example.com", "path": "/editor"}
    frame.update(change)
    with pytest.raises((ValidationError, SiteMemoryError)):
        check_entry(SiteEntryInput.model_validate(payload([frame])).model_dump())


def test_frame_depth_limit():
    frame = {"selector": "#editor", "origin": "https://example.com", "path": "/editor"}
    with pytest.raises(ValidationError):
        SiteEntryInput.model_validate(payload([frame] * 5))
