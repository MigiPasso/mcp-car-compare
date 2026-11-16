"""Preuve E0 (PLAN.md §6) : le serveur MCP parle stdio sans rien salir.

    python tests/smoke_stdio.py                      # aucun réseau, aucun pytest
    python tests/smoke_stdio.py --with-list-brands    # + un GET réel chez AS24

Ce script est le client : il démarre `python -m mcp_car_compare` en
sous-processus et vérifie quatre choses qu'aucun test unitaire ne peut voir :

1. `initialize` répond, puis `tools/list` annonce au moins trois outils, dont
   les trois de la v1 (`list_brands`, `harvest`, `offers`).
2. `tools/call offers` avec un `job_id` qui n'existe pas rend une réponse
   structurée — une erreur lisible en JSON-RPC, ou une couverture à zéro — et
   ne fait pas tomber le serveur. Un `ping` juste après le prouve : le
   processus est toujours là et parle encore le protocole.
3. **stdout ne contient que du JSON-RPC.** Chaque ligne lue sur stdout passe
   par `check_jsonrpc`, y compris avant la première réponse : une barre de
   progression, un `print` oublié ou un avertissement de bibliothèque qui
   fuit sur stdout casse ici, et pas chez l'utilisateur en pleine session.
   Le validateur est testé contre des lignes fautives connues au démarrage
   (`_self_test_validator`), sinon un validateur devenu no-op passerait ce test
   en ne refusant jamais rien.
4. **Le niveau de log ne change rien à la pureté de stdout.** Le scénario est
   rejoué avec `MCC_LOG_LEVEL=INFO` puis `MCC_LOG_LEVEL=WARNING`, où le serveur
   ne journalise plus rien : un protocole correct doit passer les deux fois.
   C'est la propriété du protocole, pas un proxy comme « stderr n'est pas
   vide » — ce dernier échouait sur un serveur parfait dès que le niveau de log
   le rendait silencieux, et ne prouvait donc rien sur stdout.

**Aucun appel réseau par défaut.** `list_brands` délègue à l'adaptateur et fait
un GET réel (~0,5 s) : une table de marques figée dériverait en silence. Un
smoke qui prouverait ce contrat dépendrait de la disponibilité d'AutoScout24,
virerait au rouge quand le site éternue, et taperait le site à chaque
exécution. Le scénario par défaut utilise donc `offers` sur un identifiant
inconnu, qui ne sort pas du conteneur. `--with-list-brands` ajoute l'appel
réel, en tolérant explicitement les deux issues (liste, ou `error`), et jamais
comme preuve de contrat.

Code de sortie non nul à la première anomalie. Les artefacts (base SQLite,
répertoire de travail du serveur) vont dans un `mkdtemp` sous /tmp : un fichier
laissé dans le dépôt casserait un `git add -A` et fausserait la relecture.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_S = 60.0
EXPECTED_TOOLS = ("list_brands", "harvest", "offers")
MAX_MESSAGES = 200
# Les deux extrêmes de verbosité du serveur : `INFO` journalise une ligne au
# démarrage, `WARNING` n'en journalise aucune. Le protocole doit sortir intact
# des deux, sinon la preuve dépend d'un réglage au lieu de dépendre du code.
LOG_LEVELS = ("INFO", "WARNING")
# Identifiant de récolte qui ne peut pas exister : le scénario par défaut doit
# rester hors réseau, donc il ne récolte rien et interroge un corpus vide.
UNKNOWN_JOB_ID = "smoke-job-inexistant"


class SmokeFailure(Exception):
    """Une anomalie du protocole. Le script s'arrête là, code de sortie 1.

    `stderr_tail` porte les dernières lignes du serveur : sans elles, un échec
    dit seulement « le serveur ne répond pas », ce qui n'aide personne à
    savoir s'il n'a pas démarré, s'il a planté, ou s'il a répondu à côté.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.stderr_tail = ""


# --- Le validateur, et sa preuve ---------------------------------------------


def check_jsonrpc(raw: bytes) -> dict:
    """Rend le message si `raw` est une ligne JSON-RPC 2.0, lève sinon.

    Volontairement strict : il ne cherche pas ce qu'il reconnaît, il exige la
    forme exacte. Un validateur permissif laisserait passer la ligne de log qui
    a fuité sur stdout, c'est-à-dire le seul défaut que ce test existe pour
    attraper.
    """
    if raw.strip() == b"":
        raise SmokeFailure(f"stdout contient une ligne vide : {raw!r}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeFailure(
            f"stdout contient des octets qui ne sont pas de l'UTF-8 : {raw[:300]!r} ({exc})"
        ) from None
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(
            f"stdout a reçu autre chose que du JSON-RPC : {raw[:300]!r} ({exc})"
        ) from None
    if not isinstance(message, dict):
        raise SmokeFailure(f"message JSON-RPC qui n'est pas un objet : {raw[:300]!r}")
    if message.get("jsonrpc") != "2.0":
        raise SmokeFailure(
            f"message sans \"jsonrpc\":\"2.0\" : {raw[:300]!r}"
        )
    is_response = "id" in message and ("result" in message or "error" in message)
    is_notification = "id" not in message and "method" in message
    if not (is_response or is_notification):
        raise SmokeFailure(
            f"objet JSON qui n'est ni une réponse ni une notification JSON-RPC : {raw[:300]!r}"
        )
    return message


def _self_test_validator() -> None:
    """Prouve que `check_jsonrpc` refuse bien, avant de lui faire confiance.

    Sans ça, un validateur qui rendrait toujours `{}` laisserait le test
    passer au vert : le seul moyen de savoir qu'il refuse, c'est de l'attaquer
    avec les lignes qu'on a réellement vues pourrir un stdout de serveur MCP.
    """
    forbidden = {
        "barre de progression": " 42%|##########        | 12/90 [00:03<00:20, 3.9it/s]\n",
        "log applicatif": "2024-11-12 10:00:00 INFO mcp_car_compare démarrage\n",
        "ligne vide": "\n",
        "jsonrpc 1.0": '{"jsonrpc": "1.0", "id": 1, "result": {}}\n',
        "jsonrpc absent": '{"id": 1, "result": {}}\n',
        "objet JSON quelconque": '{"status": "ok"}\n',
        "tableau JSON": '[{"jsonrpc": "2.0", "id": 1, "result": {}}]\n',
        "traceback": "Traceback (most recent call last):\n",
    }
    for description, text in forbidden.items():
        try:
            check_jsonrpc(text.encode("utf-8"))
        except SmokeFailure:
            continue
        raise SmokeFailure(
            f"le validateur a accepté un {description} sur stdout : il ne prouve rien"
        )
    for text in (
        '{"jsonrpc": "2.0", "id": 1, "result": {}}',
        '{"jsonrpc": "2.0", "method": "notifications/message", "params": {}}',
        '{"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "x"}}',
    ):
        check_jsonrpc(text.encode("utf-8"))


# --- Lecture de stdout sans jamais pendre ------------------------------------


class StdoutReader:
    """Lit stdout ligne par ligne, avec échéance.

    `select` plutôt qu'un `readline` bloquant : un serveur qui meurt ou qui se
    tait laisserait sinon le test pendre indéfiniment, et un test qui pend est
    indistinguable d'un serveur qui travaille. La lecture passe par
    `os.read(fd)` et non par `readline()` du fichier : un `readline` peut rendre
    une ligne partielle, et le tampon doit alors survivre à l'appel suivant.
    """

    def __init__(self, pipe) -> None:
        self._fd = pipe.fileno()
        self._buffer = bytearray()
        self.bytes_read = 0
        self.lines_read = 0

    def _fill(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    f"aucune ligne complète sur stdout en {timeout:.0f} s "
                    f"(tampon en attente : {bytes(self._buffer)[:200]!r})"
                )
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise SmokeFailure(
                    f"stdout silencieux depuis {timeout:.0f} s : le serveur ne répond pas"
                )
            chunk = os.read(self._fd, 1 << 16)
            if not chunk:
                raise SmokeFailure(
                    "stdout s'est fermé alors qu'une réponse JSON-RPC était attendue "
                    "(le serveur a-t-il planté au démarrage ? voir stderr ci-dessous)"
                )
            self.bytes_read += len(chunk)
            self._buffer.extend(chunk)

    def read_line(self, timeout: float = TIMEOUT_S) -> bytes:
        self._fill(timeout)
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        self.lines_read += 1
        return bytes(line)

    def drain_until_eof(self, timeout: float = 15.0) -> list[bytes]:
        """Vide stdout après la fin du processus, et refuse un fragment final.

        Un fragment sans saut de ligne, c'est un message tronqué : le client
        resterait à attendre une suite qui n'arrivera jamais.
        """
        lines: list[bytes] = []
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                self.lines_read += 1
                lines.append(bytes(line))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    "stdout est resté ouvert après la fermeture de stdin "
                    "(le serveur ne s'arrête pas quand le client part)"
                )
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise SmokeFailure("stdout n'a pas atteint EOF après la fermeture de stdin")
            chunk = os.read(self._fd, 1 << 16)
            if not chunk:
                break
            self.bytes_read += len(chunk)
            self._buffer.extend(chunk)
        if self._buffer:
            raise SmokeFailure(
                f"stdout se termine sur un fragment sans saut de ligne : {bytes(self._buffer)[:200]!r}"
            )
        return lines


# --- Le serveur sous test ----------------------------------------------------


class ServerUnderTest:
    """`python -m mcp_car_compare` en sous-processus, stdout sous surveillance."""

    def __init__(self, env: dict[str, str], workdir: Path) -> None:
        # cwd hors du dépôt : le lancement doit reposer sur PYTHONPATH, pas sur
        # le répertoire courant, sinon `ENTRYPOINT` ne pourrait pas faire
        # `python -m mcp_car_compare` depuis /app.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mcp_car_compare"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(workdir),
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.reader = StdoutReader(self.process.stdout)
        self._stderr_chunks: list[str] = []
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr_thread.start()
        self.leftovers: list[bytes] = []

    def _pump_stderr(self) -> None:
        """Lit stderr jusqu'à EOF dans un fil dédié.

        Sans ce fil, un serveur bavard remplirait le tuyau (64 Ko) et se
        bloquerait à l'écriture avant même d'avoir répondu : le test verrait un
        serveur muet là où il n'y a qu'un stderr qu'on ne lit pas.
        """
        assert self.process.stderr is not None
        for raw in iter(self.process.stderr.read, b""):  # pragma: no cover - boucle
            self._stderr_chunks.append(raw.decode("utf-8", "replace"))

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def next_message(self, timeout: float = TIMEOUT_S) -> dict:
        return check_jsonrpc(self.reader.read_line(timeout))

    def wait_for_response(self, request_id: int) -> dict:
        """La réponse à `request_id`, en sautant les notifications.

        Chaque message rencontré passe par `check_jsonrpc` : c'est ce qui fait
        de la lecture du protocole une vérification continue de stdout et pas
        seulement de la réponse attendue.
        """
        for _ in range(MAX_MESSAGES):
            message = self.next_message()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise SmokeFailure(
                    f"erreur JSON-RPC en réponse à id={request_id} : {message['error']}"
                )
            return message["result"]
        raise SmokeFailure(f"{MAX_MESSAGES} messages sans réponse à id={request_id}")

    def stop(self) -> int:
        """Ferme stdin, attend la sortie, vide stdout et refuse tout reste invalide."""
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise SmokeFailure(
                f"le serveur ne s'est pas arrêté {TIMEOUT_S:.0f} s après la fermeture de stdin"
            ) from None
        self.leftovers = self.reader.drain_until_eof()
        self._stderr_thread.join(timeout=5)
        return code

    def stderr_text(self) -> str:
        self._stderr_thread.join(timeout=5)
        return "".join(self._stderr_chunks)

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


# --- Le scénario -------------------------------------------------------------


def _text_of(result: dict) -> str:
    """Le texte des blocs `content` d'un `tools/call`, sans rien interpréter.

    Sert aux messages d'erreur : une erreur d'outil est du texte libre, la
    passer à `json.loads` ne rendrait qu'un second échec à la place du premier.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts) or json.dumps(result, ensure_ascii=False)


def _json_payload(result: dict) -> object:
    """La charge utile d'un `tools/call`, quelle que soit la façon dont FastMCP la range.

    mcp 1.x met le JSON dans `content[0].text` et, pour un outil qui rend un
    dict, un doublon dans `structuredContent`. Lire les deux évite de coupler
    ce test à la version du SDK.
    """
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        # `{"result": ...}` est l'enveloppe que le SDK peut ajouter autour d'un
        # retour qui n'est pas un objet.
        return structured.get("result", structured)
    text = _text_of(result)
    if not text:
        raise SmokeFailure(f"tools/call sans `content` exploitable : {result!r}"[:400])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise SmokeFailure(
            f"la charge utile de l'outil n'est pas du JSON (rend-il une chaîne ?) : {text[:300]!r}"
        ) from None


def _with_stderr(failure: SmokeFailure, server: ServerUnderTest) -> SmokeFailure:
    """Rend l'échec avec la queue de stderr du serveur attachée."""
    lines = server.stderr_text().splitlines()
    failure.stderr_tail = "\n".join(lines[-15:])
    return failure


def run_smoke(*, with_list_brands: bool = False) -> list[str]:
    """Exécute le scénario à chaque niveau de log et rend les lignes de compte rendu.

    `with_list_brands` est le seul chemin qui sort du conteneur : il interroge
    AutoScout24 pour de vrai et n'est donc pas dans la preuve par défaut. Il
    n'est joué qu'une fois, à la première passe : deux GET réels n'ajouteraient
    rien à la preuve et taperaient le site deux fois.
    """
    _self_test_validator()
    report: list[str] = []
    for level in LOG_LEVELS:
        report.extend(
            _protocol_round(
                level,
                with_list_brands=with_list_brands and level == LOG_LEVELS[0],
            )
        )
    return report


def _protocol_round(log_level: str, *, with_list_brands: bool) -> list[str]:
    """Un tour complet du protocole, à un niveau de log donné."""
    report: list[str] = []

    repo = Path(__file__).resolve().parents[1]
    workdir = Path(tempfile.mkdtemp(prefix="mcc-smoke-"))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PYTHONUNBUFFERED"] = "1"
    # La base va sous /tmp, jamais dans le dépôt ni sur le défaut de l'image
    # (/data, qui n'existe pas hors conteneur).
    env["MCC_DB"] = str(workdir / "corpus.db")
    # Le niveau est POSÉ, pas hérité : c'est ce qui fait de cette passe une
    # preuve et pas une dépendance à l'environnement de l'opérateur.
    env["MCC_LOG_LEVEL"] = log_level

    server = ServerUnderTest(env, workdir)
    body_failure: SmokeFailure | None = None
    try:
        server.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "smoke_stdio", "version": "0"},
                },
            }
        )
        init = server.wait_for_response(1)
        if not isinstance(init.get("serverInfo"), dict):
            raise SmokeFailure(f"initialize sans `serverInfo` : {init!r}"[:400])
        report.append(
            f"initialize [{log_level}]: {init['serverInfo'].get('name')} "
            f"protocole {init.get('protocolVersion')}"
        )

        # La notification `initialized` appartient au protocole : un client qui
        # l'oublie se comporte autrement selon le serveur, donc on suit la spec.
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = server.wait_for_response(2)
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise SmokeFailure(f"tools/list sans liste `tools` : {listed!r}"[:400])
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if len(tools) < 3:
            raise SmokeFailure(f"tools/list annonce {len(tools)} outil(s), 3 attendus : {names}")
        missing = [name for name in EXPECTED_TOOLS if name not in names]
        if missing:
            raise SmokeFailure(f"outils de la v1 absents de tools/list : {missing} (reçus : {names})")
        report.append(
            f"tools/list [{log_level}]: {len(tools)} outils — "
            f"{', '.join(str(n) for n in names)}"
        )

        server.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "offers", "arguments": {"job_id": UNKNOWN_JOB_ID}},
            }
        )
        offered = server.wait_for_response(3)
        # Deux issues admises, toutes deux structurées : soit le serveur nomme
        # l'erreur, soit il répond sur un corpus vide. Ce qui est interdit, c'est
        # le silence, le crash et le non-JSON sur stdout — le premier passe par
        # le `ping` ci-dessous, les deux autres par le validateur.
        if offered.get("isError"):
            message = _text_of(offered).strip()
            if not message:
                raise SmokeFailure(
                    "offers sur un job_id inconnu a rendu une erreur sans message : "
                    "l'appelant n'a rien à lire, et rien à corriger"
                )
            report.append(
                f"tools/call offers(job_id inconnu) [{log_level}]: erreur lisible — {message[:160]}"
            )
        else:
            payload = _json_payload(offered)
            if not isinstance(payload, dict) or "coverage" not in payload:
                raise SmokeFailure(
                    "offers sur un job_id inconnu ne rend ni erreur ni couverture : "
                    + json.dumps(payload, ensure_ascii=False)[:400]
                )
            coverage = payload.get("coverage")
            # Les deux compteurs du corpus, à la racine du bloc (PLAN.md §5).
            # L'ancienne version lisait une clé `corpus` que le code n'émet
            # jamais : une assertion qui ne peut pas passer ne prouve rien, et
            # une assertion sur une clé inventée ne prouve rien non plus.
            if not isinstance(coverage, dict) or not {
                "listings_distinct",
                "runs",
            } <= set(coverage):
                raise SmokeFailure(
                    "le bloc `coverage` rendu par offers n'a pas `listings_distinct` "
                    f"et `runs` : {json.dumps(coverage, ensure_ascii=False)[:400]}"
                )
            report.append(
                f"tools/call offers(job_id inconnu) [{log_level}]: couverture nulle "
                f"({coverage['listings_distinct']} annonce(s) distincte(s), "
                f"{coverage['runs']} récolte(s))"
            )

        # Le serveur doit avoir survécu à l'appel précédent : c'est la preuve
        # directe qu'un identifiant inconnu n'est pas un crash.
        server.send({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}})
        if not isinstance(server.wait_for_response(4), dict):
            raise SmokeFailure("ping n'a pas rendu un objet JSON-RPC vide")
        report.append(f"ping après l'appel [{log_level}]: serveur vivant")

        if with_list_brands:
            # Hors preuve de contrat : liste ou erreur, les deux passent. Ce
            # contrôle sert à l'opérateur qui veut voir le chemin réel, pas à
            # décider si le serveur est correct.
            server.send(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "list_brands", "arguments": {}},
                }
            )
            report.append("--with-list-brands: " + _describe_list_brands(server.wait_for_response(5)))
    except SmokeFailure as exc:
        # La panne du scénario a la priorité sur celle de l'arrêt : sinon un
        # serveur qui n'a pas voulu s'arrêter masque l'erreur qui l'a mis dans
        # cet état, et on relit le mauvais message.
        body_failure = exc

    code: int | None = None
    stop_failure: SmokeFailure | None = None
    try:
        code = server.stop()
    except SmokeFailure as exc:
        stop_failure = exc
    finally:
        server.kill()

    if body_failure is not None:
        raise _with_stderr(body_failure, server)
    if stop_failure is not None:
        raise _with_stderr(stop_failure, server)
    assert code is not None

    # Arrivé ici, stdout a été lu jusqu'à EOF : les messages restants après la
    # dernière réponse passent eux aussi par le validateur.
    for leftover in server.leftovers:
        check_jsonrpc(leftover)
    if code != 0:
        raise SmokeFailure(f"le serveur a quitté avec le code {code} après la fermeture de stdin")
    report.append(
        f"stdout [{log_level}]: {server.reader.lines_read} ligne(s), "
        f"{server.reader.bytes_read} octet(s), toutes JSON-RPC"
    )

    # stderr est LU et compté, jamais exigé : son contenu dépend du niveau de
    # log, pas de la correction du serveur. Un `WARNING` silencieux est un
    # serveur correct ; exiger une ligne ici ferait échouer ce cas et
    # n'apprendrait rien sur stdout, qui est le canal du protocole.
    stderr = server.stderr_text()
    report.append(
        f"stderr [{log_level}]: {len(stderr.splitlines())} ligne(s) — informatif, "
        "le critère est stdout"
    )
    return report


def _describe_list_brands(result: dict) -> str:
    """Décrit `list_brands` sans jamais en faire un critère de succès.

    L'outil appelle AutoScout24 : une liste, une liste vide ou un `error` sont
    trois issues légitimes selon que le site répond, change de structure ou
    refuse. En faire une assertion rendrait cette preuve dépendante d'un tiers.
    """
    if result.get("isError"):
        return f"erreur côté site ({_text_of(result).strip()[:120]})"
    try:
        payload = _json_payload(result)
    except SmokeFailure as failure:
        return f"charge utile illisible ({failure})"
    if not isinstance(payload, dict):
        return f"charge utile {type(payload).__name__}"
    if payload.get("error"):
        return f"site indisponible ({str(payload['error'])[:120]})"
    brands = payload.get("brands")
    if isinstance(brands, list):
        return f"{len(brands)} marque(s) — informatif, pas une preuve de contrat"
    return "payload sans `brands` — informatif, pas une preuve de contrat"


def main() -> int:
    arguments = sys.argv[1:]
    unknown = [arg for arg in arguments if arg != "--with-list-brands"]
    if unknown:
        print(f"argument(s) inconnu(s) : {unknown} (attendu : --with-list-brands)", file=sys.stderr)
        return 2
    try:
        report = run_smoke(with_list_brands="--with-list-brands" in arguments)
    except SmokeFailure as failure:
        print(f"SMOKE FAIL: {failure}", file=sys.stderr)
        if failure.stderr_tail:
            print("--- stderr du serveur (fin) ---", file=sys.stderr)
            print(failure.stderr_tail, file=sys.stderr)
        else:
            print("--- stderr du serveur : vide ---", file=sys.stderr)
        return 1
    print("SMOKE OK — preuve E0")
    for line in report:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
