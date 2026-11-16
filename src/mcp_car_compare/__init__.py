"""Serveur MCP local qui récolte des annonces automobiles et les expose à une IA.

Le paquet se lance par `python -m mcp_car_compare` et parle JSON-RPC sur
stdin/stdout. **stdout appartient au protocole** : tout diagnostic va sur
stderr (voir `server.py`, qui explique pourquoi on ne redirige pas le fd 1).
"""
