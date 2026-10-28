# Adjudica Aanbestedingsmonitor

Dagelijkse monitoring van openbare aanbestedingen in België, Nederland, Luxemburg en Frankrijk (TED, TenderNed, BOAMP, pmp.lu).

- **Draait automatisch** via GitHub Actions op werkdagen (zie `.github/workflows/monitor.yml`). Handmatig starten: tabblad *Actions* › *Aanbestedingsmonitor* › *Run workflow*.
- **Resultaten**: `output/data.json` (dashboard-data) en `data/aanbestedingen.db` (historiek, voorkomt dubbele meldingen).
- **Dashboard**: het online dashboard op claude.ai wordt elke werkdag bijgewerkt met `output/data.json`.
- **Profielen aanpassen**: `config.json` (CPV-codes, trefwoorden, uitsluitingen, Coming up-lijsten). Wijzig het bestand op GitHub; de volgende run gebruikt de nieuwe instellingen.

Alleen Python-standaardbibliotheek, geen extra pakketten.
