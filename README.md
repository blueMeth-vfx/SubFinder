# SubFinder

Plugin per DaVinci Resolve che **cerca una parola nei sottotitoli** della timeline
aperta e **sposta la playhead** sul sottotitolo che la contiene. Se la parola
compare più volte, due freccette **◀ ▶** scorrono tra i risultati.

## Uso
Installa `resolve_scripts/SubFinder.py` in
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`.
Lancialo da **Workspace ▸ Scripts ▸ Utility ▸ SubFinder**:

1. Apri la timeline (con una **traccia sottotitoli**) → **Aggiorna** se serve.
2. Scrivi la **parola** e premi **Cerca** (o Invio).
3. La playhead salta al primo risultato; con **◀ ▶** scorri gli altri.
   Il contatore mostra `posizione / totale`. **Doppio click** su una riga per saltarci.
4. **Parola intera**: spunta per match esatto (non "cat" dentro "category").

Self-contained, zero dipendenze, Python interno di Resolve.
