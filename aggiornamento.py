"""Controllo e applicazione degli aggiornamenti dal repository GitHub.

La cartella del gestionale e' un clone git, quindi l'aggiornamento e' un
fast-forward su origin: nessun download di tarball da scompattare a mano e il
rollback e' un 'git reset --hard <commit>' sul commit che questo modulo
restituisce prima di aggiornare.

Regole di sicurezza applicate qui:
  - se ci sono modifiche locali non committate NON si aggiorna (si perderebbero);
  - se la history locale e' divergente da origin NON si aggiorna (serve un merge
    che deve fare una persona);
  - solo --ff-only, mai un merge automatico;
  - ogni comando git ha un timeout, cosi' una rete che non risponde non blocca
    l'avvio della cassa.
"""
import os
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TIMEOUT_LOCALE = 15
TIMEOUT_RETE = 45

# Esiti possibili del controllo
AGGIORNATO = 'aggiornato'
DISPONIBILE = 'disponibile'
DIVERGENTE = 'divergente'
AVANTI = 'avanti'
NON_DISPONIBILE = 'non_disponibile'
ERRORE = 'errore'


class EsitoControllo:
    def __init__(self, stato, messaggio='', commit_locale='', commit_remoto='',
                 commits=(), modifiche_locali=()):
        self.stato = stato
        self.messaggio = messaggio
        self.commit_locale = commit_locale
        self.commit_remoto = commit_remoto
        self.commits = list(commits)
        self.modifiche_locali = list(modifiche_locali)

    @property
    def aggiornabile(self):
        """True solo se un pull --ff-only puo' andare a buon fine senza perdite."""
        return self.stato == DISPONIBILE and not self.modifiche_locali

    def __repr__(self):
        return f"<EsitoControllo {self.stato} {len(self.commits)} commit>"


def _git(*args, timeout=TIMEOUT_LOCALE):
    """Esegue un comando git nella cartella del progetto.

    Ritorna (returncode, stdout, stderr) con stdout/stderr gia' decodificati e
    ripuliti. Non solleva mai: gli errori sono valori di ritorno.
    """
    try:
        proc = subprocess.run(
            ('git',) + args,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, '', 'git non e\' installato su questo computer.'
    except subprocess.TimeoutExpired:
        return 124, '', 'Timeout: il comando git non ha risposto in tempo.'
    except OSError as err:
        return 1, '', str(err)


def git_disponibile():
    """True se git e' installato e la cartella e' effettivamente un clone."""
    rc, out, _ = _git('rev-parse', '--is-inside-work-tree')
    return rc == 0 and out == 'true'


def modifiche_locali():
    """Elenco dei file tracciati modificati e non committati."""
    rc, out, _ = _git('status', '--porcelain', '--untracked-files=no')
    if rc != 0 or not out:
        return []
    return [riga.strip() for riga in out.splitlines() if riga.strip()]


def _branch_corrente():
    rc, out, _ = _git('rev-parse', '--abbrev-ref', 'HEAD')
    return out if rc == 0 else ''


def _upstream():
    """Ref di tracking del branch corrente, con fallback su origin/main."""
    rc, out, _ = _git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if rc == 0 and out:
        return out
    branch = _branch_corrente()
    if branch and branch != 'HEAD':
        rc, _, _ = _git('rev-parse', '--verify', f'origin/{branch}')
        if rc == 0:
            return f'origin/{branch}'
    return 'origin/main'


def controlla(fetch=True):
    """Controlla se su GitHub ci sono commit piu' recenti.

    Ritorna sempre un EsitoControllo, anche in caso di errore o assenza di rete.
    """
    if not git_disponibile():
        return EsitoControllo(
            NON_DISPONIBILE,
            "Questa copia non e' un repository git (o git non e' installato): "
            "l'aggiornamento automatico non e' disponibile.",
        )

    if fetch:
        rc, _, err = _git('fetch', '--quiet', 'origin', timeout=TIMEOUT_RETE)
        if rc != 0:
            return EsitoControllo(ERRORE, f"Impossibile contattare GitHub: {err or 'errore di rete'}")

    upstream = _upstream()

    rc, locale, err = _git('rev-parse', 'HEAD')
    if rc != 0:
        return EsitoControllo(ERRORE, f"Impossibile leggere il commit locale: {err}")

    rc, remoto, err = _git('rev-parse', upstream)
    if rc != 0:
        return EsitoControllo(ERRORE, f"Riferimento remoto '{upstream}' non trovato: {err}")

    if locale == remoto:
        return EsitoControllo(AGGIORNATO, 'Il gestionale e\' gia\' aggiornato.',
                              commit_locale=locale, commit_remoto=remoto)

    rc, base, _ = _git('merge-base', 'HEAD', upstream)
    if rc != 0:
        return EsitoControllo(ERRORE, 'Impossibile confrontare la versione locale con quella remota.')

    if base == remoto:
        return EsitoControllo(
            AVANTI,
            'La copia locale ha commit non ancora pubblicati su GitHub: niente da scaricare.',
            commit_locale=locale, commit_remoto=remoto,
        )

    if base != locale:
        return EsitoControllo(
            DIVERGENTE,
            'La copia locale e quella su GitHub sono divergenti. '
            'Serve un intervento manuale (merge o rebase): non aggiorno automaticamente.',
            commit_locale=locale, commit_remoto=remoto,
        )

    rc, log, _ = _git('log', '--no-merges', '--pretty=format:%h  %ad  %s',
                      '--date=short', f'HEAD..{upstream}')
    commits = [riga for riga in log.splitlines() if riga.strip()] if rc == 0 else []

    return EsitoControllo(
        DISPONIBILE,
        f"Disponibili {len(commits)} nuovi aggiornamenti.",
        commit_locale=locale,
        commit_remoto=remoto,
        commits=commits,
        modifiche_locali=modifiche_locali(),
    )


def applica():
    """Esegue il fast-forward. Ritorna (ok, messaggio, commit_precedente).

    commit_precedente serve per il rollback: 'git reset --hard <commit>'.
    """
    esito = controlla(fetch=False)
    if esito.stato != DISPONIBILE:
        return False, esito.messaggio or 'Nessun aggiornamento da applicare.', esito.commit_locale

    sporchi = modifiche_locali()
    if sporchi:
        elenco = '\n'.join(f'  {riga}' for riga in sporchi[:10])
        return False, (
            "Ci sono modifiche locali non committate: l'aggiornamento le "
            f"sovrascriverebbe.\n\n{elenco}\n\n"
            "Committale o annullale prima di aggiornare."
        ), esito.commit_locale

    precedente = esito.commit_locale
    rc, out, err = _git('pull', '--ff-only', timeout=TIMEOUT_RETE)
    if rc != 0:
        return False, f"Aggiornamento fallito: {err or out}", precedente

    return True, f"Aggiornamento completato.\n\n{out}", precedente


def rollback(commit):
    """Riporta la copia locale al commit indicato."""
    if not commit:
        return False, 'Nessun commit di riferimento per il rollback.'
    rc, out, err = _git('reset', '--hard', commit)
    if rc != 0:
        return False, err or out
    return True, f'Ripristinata la versione {commit[:8]}.'
