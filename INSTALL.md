# Installer sur une autre machine

Deux voies. Dans les deux cas, la configuration client est la même.

## 1. Depuis le dépôt (collaborateur)

```sh
git clone git@github.com:MigiPasso/mcp-car-compare.git
cd mcp-car-compare
docker build -t mcp-car-compare:latest .      # ~5 Go, ~10 min, prévoir 15 Go libres
```

## 2. Depuis une image fournie

```sh
docker load < mcp-car-compare.tar.gz
```

Rien à construire. Côté donneur : `docker save mcp-car-compare:latest | gzip > mcp-car-compare.tar.gz` (~2 Go).

## Brancher dans un client

Ajouter dans `~/.pi/agent/mcp.json` (ou le fichier de Claude Desktop) :

```json
{
  "mcpServers": {
    "mcp-car-compare": {
      "command": "docker",
      "args": ["run", "--rm", "--interactive",
               "--volume", "mcp-car-compare-data:/data", "mcp-car-compare:latest"]
    }
  }
}
```

Puis redémarrer le client.

## Vérifier que ça marche

Demander au modèle : *« liste les marques disponibles »*. Il doit appeler `list_brands` et
rendre une liste. Si la réponse est vide avec une erreur, c'est que le conteneur ne
démarre pas — lancer la commande `docker run` à la main montre le message.

## À savoir

- **Le volume est propre à chaque machine.** `mcp-car-compare-data` est créé au premier
  lancement : deux personnes ont deux corpus séparés, sans rien partager.
- **Un corpus ne se vide pas tout seul.** Il grossit à chaque récolte ; les médianes
  portent sur tout l'historique.
- **Les tests affichent `5 skipped` sans les captures de pages**, qui ne sont pas dans le
  dépôt. C'est normal, pas une erreur.
