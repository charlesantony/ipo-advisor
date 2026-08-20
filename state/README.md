# Persistent Action state

The bootstrap workflow creates `ipo_advisor.db` here and commits it back to the
repository.

The WhatsApp recipient list is **not** stored here. It remains in the private
GitHub Actions secret `WHATSAPP_RECIPIENTS`.
