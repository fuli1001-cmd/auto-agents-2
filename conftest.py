"""Repository-only pytest lifecycle; never imported by the application."""

import pytest

pytest.register_assert_rewrite("pytest_support")

from pytest_support import tmp_path, tmp_path_factory, tmpdir_factory  # noqa: E402, F401

pytest_plugins = ["pytest_support"]
