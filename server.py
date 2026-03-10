from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from docx import Document
import uuid
import os

app = FastAPI()

FILES_DIR = "files"
os.makedirs(FILES_DIR, exist_ok=True)

class Request(BaseModel):
    patient_name: str
    vn_text: str

@app.get("/")
def root():
    return {"status": "running"}

@app.post("/create_dme_documents")
def create_docs(req: Request):

    filename = f"VN_{uuid.uuid4().hex}.docx"
    path = os.path.join(FILES_DIR, filename)

    doc = Document()
    doc.add_paragraph(req.vn_text)
    doc.save(path)

    return {
    "vn_docx_url": f"https://dme-doc-engine.onrender.com/files/{filename}"
    }

@app.get("/files/{filename}")
def get_file(filename: str):

    path = os.path.join(FILES_DIR, filename)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename
    )
