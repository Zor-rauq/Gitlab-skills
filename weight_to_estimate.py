#!/usr/bin/env python3
"""Aligne le time estimate des issues GitLab sur leur weight.

Le weight est la donnée source ; l'estimation de temps en est dérivée par un
barème explicite, obligatoire : aucune valeur implicite.

Barème (--scale)
  linéaire  --scale 4h                            1 point = 4h
  table     --scale "1=2h,2=4h,3=1d,5=2d,8=1w"    weight absent de la table : ignoré et signalé
  Conversions GitLab, fixes : 1mo = 4w, 1w = 5d, 1d = 8h.
  Un weight dont le barème donne 0 retire l'estimation existante.

Modes (--mode)
  sync  (défaut) l'estimation suit le weight, même si elle a été saisie à la main
  fill           seules les issues sans estimation sont renseignées

Garde-fous
  - Simulation par défaut : rien n'est écrit sans --apply.
  - Idempotent : seules les issues dont l'estimation diffère du barème sont modifiées ;
    chaque modification laisse une note système signée par le compte du token (audit).
  - Token lu uniquement dans GITLAB_TOKEN, jamais en argument (visible dans ps et les logs).
  - Avertissement si le token expire dans moins de 30 jours.

Environnement
  GITLAB_URL        URL de l'instance (à défaut : CI_SERVER_URL)
  GITLAB_TOKEN      group access token dédié, scope api, rôle Planner
  GITLAB_CA_BUNDLE  bundle CA contenant la CA interne (à défaut : REQUESTS_CA_BUNDLE ;
                    sans l'un ni l'autre, requests utilise certifi, qui ne la contient pas)

Exemples
  python3 weight_to_estimate.py --group plateforme --scale 4h
  python3 weight_to_estimate.py --group plateforme --scale 4h --apply
  python3 weight_to_estimate.py --project grp/svc-referentiel \\
      --scale "1=2h,2=4h,3=1d,5=2d,8=1w" --mode fill --apply

Codes de sortie : 0 succès, 1 au moins une issue en erreur, 2 configuration ou accès invalide.
Prérequis : Python >= 3.10, python-gitlab 8.x (dépendances : requests, requests-toolbelt).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Callable, NoReturn, Optional

import gitlab
import requests

__version__ = "1.0.0"

# Conversions GitLab, non configurables : 1mo = 4w, 1w = 5d, 1d = 8h.
UNIT_SECONDS = {"mo": 576_000, "w": 144_000, "d": 28_800, "h": 3_600, "m": 60}
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(mo|w|d|h|m)")
TOKEN_EXPIRY_WARNING_DAYS = 30

Scale = Callable[[int], Optional[int]]

# Clé de comptage -> libellé du bilan
SUMMARY = (
    ("simulation", "à mettre à jour"),
    ("appliqué", "mises à jour"),
    ("inchangée", "inchangées"),
    ("conservée", "conservées (mode fill)"),
    ("hors barème", "hors barème"),
    ("sans weight", "sans weight"),
    ("erreur", "erreurs"),
)


# --------------------------------------------------------------------------- durées

def parse_duration(text: str) -> int:
    """'4h', '1d', '2h30m', '1w 2d', '1.5h' -> secondes, arrondies à la minute."""
    compact = text.strip().lower().replace(" ", "")
    if compact == "0":
        return 0
    total, pos = 0.0, 0
    for match in _DURATION_TOKEN.finditer(compact):
        if match.start() != pos:
            break
        total += float(match.group(1)) * UNIT_SECONDS[match.group(2)]
        pos = match.end()
    if not compact or pos != len(compact):
        raise ValueError(f"durée invalide : {text!r} (exemples : 30m, 4h, 1d, 2h30m, 1w)")
    return round(total / 60) * 60


def humanize(seconds: int) -> str:
    """Affichage façon GitLab : 72000 -> '2d 4h'."""
    if not seconds:
        return "0"
    parts, rest = [], seconds
    for unit in ("w", "d", "h", "m"):
        quantity, rest = divmod(rest, UNIT_SECONDS[unit])
        if quantity:
            parts.append(f"{quantity}{unit}")
    return " ".join(parts) or f"{seconds}s"


def to_gitlab_duration(seconds: int) -> str:
    """Secondes -> '20h', '1h30m', '45m' : heures et minutes seulement, aucune ambiguïté."""
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


# --------------------------------------------------------------------------- barème

def build_scale(spec: str) -> tuple[Scale, str]:
    """'4h' (linéaire) ou '1=2h,2=4h,3=1d' (table) -> (weight -> secondes, libellé)."""
    if "=" not in spec:
        per_point = parse_duration(spec)
        if per_point <= 0:
            raise ValueError("un barème linéaire doit être strictement positif")
        return (lambda weight: weight * per_point), f"1 point = {humanize(per_point)}"

    table: dict[int, int] = {}
    for item in spec.split(","):
        key, _, value = item.partition("=")
        if not key.strip().isdigit() or not value.strip():
            raise ValueError(f"entrée invalide : {item!r} (attendu weight=durée, ex. 3=1d)")
        weight = int(key)
        if weight in table:
            raise ValueError(f"weight {weight} défini deux fois")
        table[weight] = parse_duration(value)
    label = ", ".join(f"{w}={humanize(s)}" for w, s in sorted(table.items()))
    return table.get, f"table {label}"


def decide(weight: int, current: int, scale: Scale, mode: str) -> tuple[str, Optional[int]]:
    """-> ('hors barème' | 'inchangée' | 'conservée' | 'à écrire', estimation cible)."""
    target = scale(weight)
    if target is None:
        return "hors barème", None
    if current == target:
        return "inchangée", target
    if mode == "fill" and current:
        return "conservée", target
    return "à écrire", target


# --------------------------------------------------------------------------- sorties

def fail(message: str) -> NoReturn:
    print(f"Erreur : {message}", file=sys.stderr, flush=True)
    sys.exit(2)


def warn(message: str) -> None:
    print(f"Avertissement : {message}", file=sys.stderr, flush=True)


def report(tag: str, ref: str, weight: int, current: int, target: Optional[int]) -> None:
    before = humanize(current) if current else "aucune"
    if target is None:
        after = "weight absent du barème, issue ignorée"
    else:
        after = humanize(target) if target else "aucune"
    print(f"[{tag}] {ref}  weight {weight}  estimate {before} -> {after}", flush=True)


# --------------------------------------------------------------------------- GitLab

def connect(args: argparse.Namespace) -> gitlab.Gitlab:
    url = args.url or os.environ.get("GITLAB_URL") or os.environ.get("CI_SERVER_URL")
    token = os.environ.get("GITLAB_TOKEN")
    ca_bundle = args.ca_bundle or os.environ.get("GITLAB_CA_BUNDLE")
    if not url:
        fail("URL GitLab absente (--url, GITLAB_URL ou CI_SERVER_URL)")
    if not token:
        fail("GITLAB_TOKEN absent (le token n'est jamais accepté en argument)")
    if ca_bundle and not os.path.isfile(ca_bundle):
        fail(f"bundle CA introuvable : {ca_bundle}")

    gl = gitlab.Gitlab(
        url,
        private_token=token,
        ssl_verify=ca_bundle or True,  # chemin du bundle, sinon REQUESTS_CA_BUNDLE puis certifi
        timeout=30,
        retry_transient_errors=False,  # premier appel : échec immédiat et explicite
        keep_base_url=True,  # pagination : rester sur l'URL fournie si l'external_url diffère
        user_agent=f"weight-to-estimate/{__version__}",  # identifiable dans les logs GitLab
    )
    try:
        gl.auth()
    except requests.exceptions.SSLError as exc:
        fail(f"échec TLS vers {url} : CA interne absente du bundle ? (GITLAB_CA_BUNDLE) — {exc}")
    except requests.exceptions.ProxyError as exc:
        fail(f"requête partie vers le proxy sortant : ajouter l'hôte GitLab à NO_PROXY — {exc}")
    except requests.exceptions.RequestException as exc:
        fail(f"GitLab injoignable ({url}) : DNS, flux firewall ou NO_PROXY — {exc}")
    except gitlab.exceptions.GitlabAuthenticationError:
        fail("authentification refusée : token expiré, révoqué ou invalide")
    gl.retry_transient_errors = True  # ensuite : résilience aux 5xx et aux coupures brèves
    return gl


def check_token(gl: gitlab.Gitlab, apply: bool) -> None:
    """Scope api requis pour écrire ; alerte avant expiration (panne silencieuse classique)."""
    try:
        info = gl.http_get("/personal_access_tokens/self")
    except gitlab.exceptions.GitlabError as exc:
        warn(f"vérification du token impossible ({exc}), poursuite")
        return
    if "api" not in info.get("scopes", []):
        message = "le token n'a pas le scope api : l'écriture des estimations sera refusée"
        if apply:
            fail(message)
        warn(message)
    expires_at = info.get("expires_at")
    if expires_at:
        days_left = (date.fromisoformat(expires_at[:10]) - date.today()).days
        if days_left <= TOKEN_EXPIRY_WARNING_DAYS:
            warn(f"le token expire dans {days_left} jour(s) ({expires_at}) : rotation à planifier")


def write_estimate(gl: gitlab.Gitlab, project_id: int, iid: int, target: int) -> None:
    # GroupIssue est en lecture seule dans python-gitlab : l'écriture passe par une
    # ProjectIssue instanciée en « lazy », sans GET supplémentaire.
    issue = gl.projects.get(project_id, lazy=True).issues.get(iid, lazy=True)
    if target:
        issue.time_estimate(to_gitlab_duration(target))
    else:
        issue.reset_time_estimate()


def process(gl: gitlab.Gitlab, issue, scale: Scale, args: argparse.Namespace,
            stats: Counter) -> None:
    attrs = issue.attributes
    ref = (attrs.get("references") or {}).get("full") or f"{attrs['project_id']}#{attrs['iid']}"
    weight = attrs.get("weight")
    if weight is None:  # filtre weight=Any non appliqué par l'instance : on ne touche à rien
        stats["sans weight"] += 1
        return
    current = (attrs.get("time_stats") or {}).get("time_estimate") or 0
    status, target = decide(weight, current, scale, args.mode)

    if status != "à écrire":
        stats[status] += 1
        if status == "hors barème" or args.verbose:
            report(status, ref, weight, current, target)
        return
    if not args.apply:
        stats["simulation"] += 1
        report("simulation", ref, weight, current, target)
        return
    try:
        write_estimate(gl, attrs["project_id"], attrs["iid"], target)
    except gitlab.exceptions.GitlabError as exc:
        stats["erreur"] += 1
        print(f"[erreur] {ref} : {exc}", file=sys.stderr, flush=True)
        return
    stats["appliqué"] += 1
    report("appliqué", ref, weight, current, target)


# --------------------------------------------------------------------------- CLI

def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("doit être un entier strictement positif")
    return value


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aligne le time estimate des issues GitLab sur leur weight.",
        epilog="Simulation par défaut. Détails : en-tête du script.",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--group", help="chemin du groupe, sous-groupes inclus (ex. plateforme)")
    scope.add_argument("--project", help="chemin du projet (ex. grp/svc-referentiel)")
    parser.add_argument("--scale", required=True,
                        help="barème : '4h' (linéaire) ou '1=2h,2=4h,3=1d,5=2d,8=1w' (table)")
    parser.add_argument("--mode", choices=("sync", "fill"), default="sync",
                        help="sync : l'estimation suit le weight (défaut) ; "
                             "fill : seules les issues sans estimation")
    parser.add_argument("--state", choices=("opened", "closed", "all"), default="opened")
    parser.add_argument("--labels", help="labels requis, séparés par des virgules")
    parser.add_argument("--since-hours", type=positive_int,
                        help="ne traiter que les issues modifiées depuis N heures")
    parser.add_argument("--apply", action="store_true", help="écrire (sinon simulation)")
    parser.add_argument("--url", help="URL GitLab (défaut : GITLAB_URL, puis CI_SERVER_URL)")
    parser.add_argument("--ca-bundle", help="bundle CA (défaut : GITLAB_CA_BUNDLE)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="lister aussi les issues inchangées ou conservées")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not (args.group or args.project):
        fail("périmètre vide : --group ou --project sans valeur (variable CI absente ?)")
    try:
        scale, scale_label = build_scale(args.scale)
    except ValueError as exc:
        fail(f"barème : {exc}")

    gl = connect(args)
    check_token(gl, args.apply)

    path = args.group or args.project
    kind = "groupe" if args.group else "projet"
    try:
        scope = gl.groups.get(path) if args.group else gl.projects.get(path)
    except gitlab.exceptions.GitlabGetError as exc:
        fail(f"{kind} {path} introuvable ou non visible par le token ({exc.response_code})")

    filters = {
        "weight": "Any",  # filtrage côté API (Premium/Ultimate)
        "order_by": "created_at",  # ordre stable : écrire l'estimation modifie updated_at
        "sort": "asc",
        "per_page": 100,
        "iterator": True,
    }
    if args.state != "all":
        filters["state"] = args.state
    if args.labels:
        filters["labels"] = args.labels
    if args.since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
        filters["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    run_mode = "APPLICATION" if args.apply else "SIMULATION (aucune écriture)"
    print(f"GitLab {gl.url} — compte @{gl.user.username}", flush=True)
    print(f"Périmètre : {kind} {path} · issues {args.state} · barème {scale_label} · "
          f"mode {args.mode} · {run_mode}", flush=True)

    stats: Counter = Counter()
    try:
        for issue in scope.issues.list(**filters):
            stats["lue"] += 1
            process(gl, issue, scale, args, stats)
    except (gitlab.exceptions.GitlabError, requests.exceptions.RequestException) as exc:
        stats["erreur"] += 1
        print(f"Interruption : {exc} — relancer, le traitement est idempotent.",
              file=sys.stderr, flush=True)

    details = [f"{label} : {stats[key]}" for key, label in SUMMARY if stats[key]]
    print("Bilan — " + " · ".join([f"issues lues : {stats['lue']}", *details]), flush=True)
    if not args.apply and stats["simulation"]:
        print("Simulation : aucune écriture. Relancer avec --apply pour appliquer.", flush=True)
    return 1 if stats["erreur"] else 0


if __name__ == "__main__":
    sys.exit(main())
