"""Worker « Un Chiffre, Un Geste » — vidéo HTML → MP4 → Google Drive.

Chaîne : je (Vibe) déclenche le workflow `ucug-video` avec le HTML du canvas
vidéo et, optionnellement, les segments vocaux MP3 (base64). Le worker :
1. capture l'animation (renderer.py) et produit le MP4 1080×1920 · H.264 · 30 fps ;
2. livre le fichier dans le Google Drive de l'utilisateur déclencheur (OBO),
   via le Connector Drive — le MP4 ne transite jamais par la sortie du workflow
   (limite 2 Mo) ; le workflow ne retourne que des métadonnées.
"""

import asyncio
import base64
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pydantic
import mistralai.workflows as workflows
from mistralai.workflows import Depends, activity_heartbeat
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

import renderer

# Nom du Connector Drive tel qu'enregistré dans le Studio de l'utilisateur.
DRIVE_CONNECTOR_NAME = os.environ.get("DRIVE_CONNECTOR_NAME", "google_drive_mcp")
drive_connector = connector(DRIVE_CONNECTOR_NAME)


class VideoRequest(pydantic.BaseModel):
    """Entrée du workflow (JSON)."""

    html: str                                    # HTML du canvas (frontmatter YAML optionnel)
    voice_segments_b64: List[str] = pydantic.Field(default_factory=list)
    title: str = "video-un-chiffre-un-geste.mp4"  # nom du fichier livré dans le Drive
    folder_id: Optional[str] = None               # dossier Drive cible (racine par défaut)
    crf: int = 23                                 # qualité x264 (18 = max, 23 = standard)


def strip_frontmatter(canvas_md: str) -> str:
    """Retire l'éventuel frontmatter YAML d'un fichier CANVAS.md Work."""
    text = canvas_md.lstrip("\ufeff")
    if text.startswith("---"):
        lines = text.splitlines()
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).lstrip()
    return text


def _drive_info(result: Any) -> Dict[str, Any]:
    """Extrait id / titre / URL de la réponse MCP create_file (forme variable)."""

    def walk(obj: Any) -> Optional[Dict[str, Any]]:
        if isinstance(obj, dict):
            if any(k.lower() == "id" for k in obj):
                return obj
            for v in obj.values():
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = walk(v)
                if found is not None:
                    return found
        return None

    node = walk(result) or {}
    low = {k.lower(): k for k in node}

    def pick(*names: str) -> Optional[Any]:
        for n in names:
            if n in low:
                return node[low[n]]
        return None

    return {
        "drive_file_id": pick("id"),
        "drive_title": pick("title", "name", "filename"),
        "drive_url": pick("view_url", "url", "web_view_link", "link"),
    }


@workflows.activity(
    name="render-and-upload",
    start_to_close_timeout=timedelta(minutes=15),
    retry_policy_max_attempts=2,
)
async def render_and_upload(
    req: VideoRequest,
    drive: ToolCallClient = Depends(drive_connector),
) -> Dict[str, Any]:
    """Rend le MP4 puis le livre sur le Drive de l'utilisateur déclencheur."""

    def beat(step: str) -> None:
        activity_heartbeat({"step": step})

    with tempfile.TemporaryDirectory(prefix="ucug-") as tmp:
        tmpdir = Path(tmp)

        # 0. Préparation des entrées
        html_path = tmpdir / "video.html"
        html_path.write_text(strip_frontmatter(req.html), encoding="utf-8")

        voice_paths: List[Path] = []
        for i, b64 in enumerate(req.voice_segments_b64, start=1):
            seg = tmpdir / ("segment%d.mp3" % i)
            seg.write_bytes(base64.b64decode(b64))
            voice_paths.append(seg)

        # 1. Capture + transcodage
        beat("capture de l'animation")
        webm, starts, total = await renderer.capture_animation(
            html_path, tmpdir, progress=beat
        )
        beat("transcodage MP4")
        muet = tmpdir / "muet.mp4"
        await renderer.transcode(webm, muet, crf=req.crf)

        # 2. Assemblage voix (optionnel)
        if voice_paths:
            beat("assemblage de la voix (%d segments)" % len(voice_paths))
            final = tmpdir / "final.mp4"
            await renderer.mux_with_voice(muet, voice_paths, starts, total, final)
        else:
            final = tmpdir / "final.mp4"
            await renderer.remux_faststart(muet, final)

        data = final.read_bytes()
        uploaded_mb = round(len(data) / 1_000_000, 1)

        # 3. Livraison Drive (OBO : credentials de l'utilisateur déclencheur)
        def upload_args(payload_b64: str) -> Dict[str, Any]:
            args: Dict[str, Any] = {
                "title": req.title,
                "base64_content": payload_b64,
                "content_mime_type": "video/mp4",
            }
            if req.folder_id:
                args["parent_id"] = req.folder_id
            return args

        beat("upload Drive (%s Mo)" % uploaded_mb)
        result = None
        try:
            result = await drive.call_tool(
                tool_name="create_file",
                arguments=upload_args(base64.b64encode(data).decode("ascii")),
            )
        except Exception:
            # Fallback : si le payload est refusé (trop volumineux pour l'outil),
            # ré-encode en 720×1280 CRF 28 (≈ 2× plus léger) et retente une fois.
            beat("upload : nouvelle tentative en 720p")
            small = tmpdir / "small.mp4"
            await renderer.transcode(webm, small, crf=28, scale_filter="scale=720:1280")
            if voice_paths:
                small_final = tmpdir / "small-voix.mp4"
                await renderer.mux_with_voice(small, voice_paths, starts, total, small_final)
                data = small_final.read_bytes()
            else:
                data = small.read_bytes()
            uploaded_mb = round(len(data) / 1_000_000, 1)
            result = await drive.call_tool(
                tool_name="create_file",
                arguments=upload_args(base64.b64encode(data).decode("ascii")),
            )

    info = _drive_info(result)
    info.update(
        duration_s=total,
        scenes=len(starts),
        voice_segments=len(voice_paths),
        uploaded_mb=uploaded_mb,
    )
    return info


@workflows.workflow.define(
    name="ucug-video",
    workflow_display_name="Un Chiffre Un Geste — rendu vidéo",
    workflow_description=(
        "Rend le MP4 1080x1920 d'une vidéo HTML (canvas Work), avec segments "
        "vocaux MP3 optionnels alignes sur les scenes, et le livre sur le "
        "Google Drive de l'utilisateur declencheur."
    ),
    on_behalf_of=True,
)
@uses_connectors(drive_connector)
class UcugVideoWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, req: VideoRequest) -> Dict[str, Any]:
        return await render_and_upload(req)


async def main() -> None:
    await workflows.run_worker([UcugVideoWorkflow])


if __name__ == "__main__":
    asyncio.run(main())
