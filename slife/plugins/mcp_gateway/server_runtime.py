"""mcp_plugin server bootstrap — bind to the shared slife implementation.

Historically this module was a slife-free *copy* of the plugin-bootstrap
subset of :mod:`slife.server_utils` (per-session logging, port binding, the
ready port signal, and the ``create_plugin_server`` / ``run_plugin_server``
pair).  mcp-plugin is a built-in slife plugin now, so the copy is gone —
every symbol is re-exported from the one canonical implementation, and the
single functional drift the copy carried (honoring ``SLIFE_PLUGIN_NAME``
for the per-session log-file suffix) lives in
``slife.server_utils.create_plugin_server``.
"""

from slife import server_utils as _server_utils

# Module-level assignment re-exports (ruff F401 / pyright reportUnusedImport
# see each name as a public attribute while the import itself is used).
bind_free_port = _server_utils.bind_free_port
create_plugin_server = _server_utils.create_plugin_server
install_uncaught_exception_cleanup = _server_utils.install_uncaught_exception_cleanup
run_plugin_server = _server_utils.run_plugin_server
setup_server_logging = _server_utils.setup_server_logging
shutdown_server_logging = _server_utils.shutdown_server_logging
signal_port = _server_utils.signal_port