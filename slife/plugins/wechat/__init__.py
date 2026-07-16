"""slife-wechat — optional WeChat iLink ClawBot plugin.

Provides WeChat messaging integration via the iLink ClawBot protocol.
Session tokens are stored per-user in ``wechat_<user>.json5``.

Enabled via ``slife.json5``::

    wechat: { enabled: true }

LLM-visible tools:
  - wechat_login       — QR code login
  - wechat_send_message — send text to a WeChat user
  - wechat_check_status — check login/session status
  - wechat_logout      — clear session
"""
