# Un Chiffre, Un Geste — worker de rendu vidéo

Workflow Mistral (déploiement géré) qui transforme le canvas vidéo HTML du
compte Instagram/TikTok **@unchiffreungeste** en MP4 publiable, sans tournage,
sans éditeur vidéo, sans filigrane :

```
HTML (canvas Work) + segments vocaux MP3 (optionnel, base64)
        │
        ▼  Playwright (capture 1080×1920) + ffmpeg (H.264 · 30 fps)
      MP4  ──►  Google Drive de l'utilisateur déclencheur (Connector, OBO)
```

## Workflow `ucug-video`

Entrée (JSON) :

| Champ                | Type           | Défaut                     | Rôle                                    |
| -------------------- | -------------- | -------------------------- | --------------------------------------- |
| `html`               | `str`          | —                          | HTML du canvas (frontmatter YAML toléré) |
| `voice_segments_b64` | `list[str]`    | `[]`                       | MP3 base64, alignés sur les scènes      |
| `title`              | `str`          | `video-un-chiffre…mp4`     | Nom du fichier livré dans le Drive      |
| `folder_id`          | `str \| null`  | `null` (racine)            | Dossier Drive cible                     |
| `crf`                | `int`          | `23`                       | Qualité x264                            |

Sortie : métadonnées seulement (`drive_file_id`, `drive_url`, `duration_s`,
`uploaded_mb`…) — le MP4 ne transite jamais par le workflow (limite 2 Mo),
il est téléversé directement depuis le worker.

## Détails techniques

- Les durées de scènes sont lues **dans le HTML** (variable `SCENES`) : aucune
  constante à maintenir d'une vidéo à l'autre.
- Voix : chaque segment est décalé au timecode de sa scène (`adelay`) puis
  mélangé (`amix`), piste AAC 192 kb/s.
- Fallback : si l'upload Drive est refusé (payload trop volumineux), le worker
  ré-encode en 720×1280 CRF 28 et retente.
- `on_behalf_of=True` : le fichier atterrit dans le Drive de l'utilisateur qui
  déclenche l'exécution (déploiement durci requis).
- Le nom du Connector Drive est surchargeable : `DRIVE_CONNECTOR_NAME`
  (défaut `google_drive_mcp`), à ajuster si le vôtre est enregistré sous un
  autre nom dans Studio › Context › Connectors.
