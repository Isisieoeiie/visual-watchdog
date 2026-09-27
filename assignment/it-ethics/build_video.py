#!/usr/bin/env python3
"""Build a narrated Thai slideshow video from index.html."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from gtts import gTTS

ROOT = Path(__file__).resolve().parent
OUT = Path("/tmp/it-ethics-video")
FRAMES = OUT / "frames"
AUDIO = OUT / "audio"
FINAL = Path("/opt/cursor/artifacts/it_ethics_deepfake_presentation.mp4")
BASE = "http://127.0.0.1:8765"
FONT = str(ROOT / "fonts" / "Sarabun-Regular.ttf")

NARRATION = [
    "สวัสดีค่ะ ดิฉันจะนำเสนอหัวข้อ จริยธรรมและจรรยาบรรณในการใช้เทคโนโลยีสารสนเทศ พร้อมข่าวหนึ่งเรื่องที่กำลังเกิดจริงในประเทศไทย คือการใช้เอไอ ดีปเฟค และวอยซ์โคลน เพื่อหลอกโอนเงิน",
    "วันนี้จะพูดสามส่วนค่ะ. หนึ่ง ความหมายของจริยธรรมและจรรยาบรรณ. สอง หลักการใช้เทคโนโลยีอย่างรับผิดชอบ และจรรยาบรรณวิชาชีพ. สาม วิเคราะห์ข่าวจริง แล้วปิดท้ายด้วยสิ่งที่นักเรียนทำได้ทันที",
    "จริยธรรม คือหลักที่บอกว่าอะไรถูกอะไรผิด เป็นสิ่งที่สังคมยึดถือ แม้ไม่มีใครยืนบังคับ เช่น ไม่โกหก ไม่รังแก ไม่เอาข้อมูลคนอื่นไปใช้. จรรยาบรรณ คือกฎความประพฤติของคนในวิชาชีพ เช่น โปรแกรมเมอร์ ต้องรักษาความลับ และไม่สร้างโปรแกรมที่เป็นภัย. สิ่งสำคัญคือ เทคโนโลยีไม่มีจริยธรรมในตัว คนเป็นคนเลือกใช้",
    "หลักจริยธรรมที่ควรจำมีหกข้อค่ะ. หนึ่ง ไม่ใช้เทคโนโลยีทำร้ายผู้อื่น. สอง เคารพความเป็นส่วนตัว. สาม ไม่ขโมยผลงาน. สี่ ไม่เผยแพร่ข้อมูลเท็จ. ห้า รับผิดชอบต่อสิ่งที่โพสต์. หก คิดถึงผลกระทบต่อสังคมก่อนกดส่ง",
    "จรรยาบรรณวิชาชีพคอมพิวเตอร์ เน้นสามเรื่องค่ะ. ยึดประโยชน์ของสาธารณะมาก่อน. ซื่อสัตย์และรักษาความลับของข้อมูล. และไม่สร้างเครื่องมือที่เป็นภัย เช่น มัลแวร์ หรือโปรแกรมที่ใช้หลอกเงิน",
    "แม้หัวข้อนี้จะเน้นจริยธรรม แต่กฎหมายคือเส้นสุดท้ายค่ะ. พระราชบัญญัติคอมพิวเตอร์ ห้ามนำเข้าข้อมูลเท็จ และห้ามนำภาพคนอื่นไปตัดต่อจนเสียชื่อเสียง ซึ่งใช้กับดีปเฟคได้บางกรณี. ส่วน พีดีพีเอ คุ้มครองใบหน้าและเสียงของเรา",
    "ข่าวที่นำมาวิเคราะห์ คือข่าวของกระทรวงดิจิทัลเพื่อเศรษฐกิจและสังคม เมื่อวันที่เก้า กุมภาพันธ์ สองพันห้าร้อยหกสิบเก้า. ดีอีเตือนว่ามิจฉาชีพใช้เอไอ เลียนแบบเสียงพ่อ แม่ ลูก หลาน หรือเจ้าหน้าที่รัฐ แล้วสร้างเรื่องเร่งด่วนให้เหยื่อโอนเงินทันที",
    "มีกรณีตัวอย่างจากศูนย์ เอโอซี หนึ่งสี่สี่หนึ่ง ค่ะ. ผู้เสียหายได้รับสายจากคนที่อ้างว่าเป็นลูกสาวที่เรียนมหาวิทยาลัย เสียงเหมือนจริง จึงโอนไปกว่าหนึ่งแสนบาท. ภายหลังโทรถามลูกสาวตัวจริง จึงรู้ว่าเสียงถูกปลอมด้วยเอไอ. ไทยพีบีเอส เวอริฟาย ยังรายงานว่า ปีสองพันห้าร้อยหกสิบแปด มิจฉาชีพใช้การปลอมเสียงมากขึ้น",
    "ข่าวนี้ผิดหลักจริยธรรมหลายข้อค่ะ. ไม่ซื่อสัตย์ เพราะแอบอ้างเป็นคนในครอบครัว. ละเมิดความเป็นส่วนตัว เพราะเอาเสียงและใบหน้าไปใช้โดยไม่ได้รับอนุญาต. และใช้เทคโนโลยีทำร้ายสังคม เพราะทำลายความไว้ใจในครอบครัว",
    "สิ่งที่เราทำได้ตั้งแต่วันนี้มีสี่อย่างค่ะ. ตั้งรหัสลับในครอบครัว. ถ้าเบอร์ไม่คุ้น ให้วางสายแล้วโทรกลับเบอร์เดิม. ยึดหลัก ไม่กดลิงก์ ไม่เชื่อ ไม่รีบ ไม่โอน. และถ้าถูกหลอก ให้โทร เอโอซี หนึ่งสี่สี่หนึ่ง",
    "สรุปค่ะ. เทคโนโลยีไม่มีจริยธรรมในตัว คนเป็นคนเลือกใช้. จริยธรรมบอกว่าอะไรถูก. จรรยาบรรณบอกว่าวิชาชีพควรทำอย่างไร. และกฎหมายคือเส้นสุดท้ายเมื่อมีคนละเมิด. ข่าวดีปเฟคสอนเราว่า ความรู้คอมพิวเตอร์ถ้าไม่มีคุณธรรม จะกลายเป็นอาวุธได้ทันที",
    "ข้อมูลในคลิปนี้อ้างอิงจากกระทรวงดีอี, ไทยพีบีเอส เวอริฟาย, สภาองค์กรของผู้บริโภค, จรรยาบรรณเอซีเอ็ม และกฎหมายไทยที่เกี่ยวข้อง. ขอบคุณที่รับฟังค่ะ",
]


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def duration(path: Path) -> float:
    raw = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    return float(json.loads(raw)["format"]["duration"])


def main() -> None:
    FRAMES.mkdir(parents=True, exist_ok=True)
    AUDIO.mkdir(parents=True, exist_ok=True)
    FINAL.parent.mkdir(parents=True, exist_ok=True)

    clips = []
    for i, text in enumerate(NARRATION):
        mp3 = AUDIO / f"{i:02d}.mp3"
        if not mp3.exists():
            print(f"tts {i}")
            gTTS(text=text, lang="th").save(str(mp3))
            time.sleep(0.4)

        png = FRAMES / f"{i:02d}.png"
        if not png.exists():
            print(f"shot {i}")
            shot = subprocess.run(
                [
                    "timeout",
                    "12",
                    "google-chrome",
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--remote-debugging-port=0",
                    f"--user-data-dir=/tmp/chrome-ethics-{i}",
                    "--window-size=1920,1080",
                    "--virtual-time-budget=3000",
                    f"--screenshot={png}",
                    f"{BASE}/?slide={i}&record=1",
                ]
            )
            if not png.exists():
                raise RuntimeError(f"screenshot failed for slide {i}: {shot.returncode}")

        wav_clip = OUT / f"clip_{i:02d}.mp4"
        sec = duration(mp3) + 0.8
        print(f"clip {i} {sec:.1f}s")
        run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(png),
                "-i",
                str(mp3),
                "-c:v",
                "libx264",
                "-tune",
                "stillimage",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                "-t",
                f"{sec:.2f}",
                "-shortest",
                str(wav_clip),
            ]
        )
        clips.append(wav_clip)

    concat = OUT / "concat.txt"
    concat.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    print("concat")
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            str(FINAL),
        ]
    )
    print("wrote", FINAL, "size", FINAL.stat().st_size)


if __name__ == "__main__":
    main()
