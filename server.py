from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
from docxtpl import DocxTemplate
import uuid
import os
import zipfile

app = FastAPI()

BASE_URL = "https://dme-doc-engine.onrender.com"

OUTPUT_DIR = "output"
TEMPLATES_DIR = "templates"

os.makedirs(OUTPUT_DIR, exist_ok=True)


class Diagnosis(BaseModel):
    code: str
    label: str


class Vitals(BaseModel):
    height: str
    weight: str
    blood_pressure: str
    pulse: str
    respiration: str
    temperature: str


class OrderItem(BaseModel):
    items: List[str]
    icd10: List[str]


class Payload(BaseModel):

    patient_name: str
    dob: str
    age: str
    sex: str

    physician_name: str
    practice_address: str
    practice_phone: str
    practice_fax: str

    facility_name: str
    facility_address: str
    facility_phone: str

    exam_date: str
    signature_date: str

    vitals: Vitals

    diagnoses: List[Diagnosis]

    equipment_list: List[str]

    vn_text: str

    orders: List[OrderItem]


def validate_docx(path):

    if not zipfile.is_zipfile(path):
        raise Exception("Invalid DOCX structure")

    with zipfile.ZipFile(path) as z:

        required = [
            "[Content_Types].xml",
            "word/document.xml"
        ]

        names = z.namelist()

        for r in required:
            if r not in names:
                raise Exception("Corrupted DOCX")


def generate_vn(payload):

    template_path = os.path.join(TEMPLATES_DIR, "MASTER_VN.docx")

    template = DocxTemplate(template_path)

    primary_dx = payload.diagnoses[0].label

    secondary_dx = ", ".join(
        [d.label for d in payload.diagnoses[1:]]
    )

    context = {

        "physician_name": payload.physician_name,
        "practice_address": payload.practice_address,
        "practice_phone": payload.practice_phone,
        "practice_fax": payload.practice_fax,

        "exam_date": payload.exam_date,

        "patient_name": payload.patient_name,
        "dob": payload.dob,
        "age": payload.age,
        "sex": payload.sex,

        "facility_name": payload.facility_name,
        "facility_address": payload.facility_address,
        "facility_phone": payload.facility_phone,

        "height": payload.vitals.height,
        "weight": payload.vitals.weight,
        "blood_pressure": payload.vitals.blood_pressure,
        "pulse": payload.vitals.pulse,
        "respiration": payload.vitals.respiration,
        "temperature": payload.vitals.temperature,

        "primary_diagnosis": primary_dx,
        "secondary_diagnoses": secondary_dx
    }

    filename = f"VN_{uuid.uuid4().hex}.docx"

    path = os.path.join(OUTPUT_DIR, filename)

    template.render(context)
    template.save(path)

    validate_docx(path)

    return filename


def generate_orders(payload):

    files = []

    template_path = os.path.join(TEMPLATES_DIR, "MASTER_ORDER.docx")

    for order in payload.orders:

        template = DocxTemplate(template_path)

        context = {

            "physician_name": payload.physician_name,
            "practice_address": payload.practice_address,
            "practice_phone": payload.practice_phone,
            "practice_fax": payload.practice_fax,

            "patient_name": payload.patient_name,

            "equipment_1_name": order.items[0] if len(order.items) > 0 else "",
            "equipment_2_name": order.items[1] if len(order.items) > 1 else "",

            "diagnosis_text": ", ".join([d.label for d in payload.diagnoses]),

            "icd_codes": ", ".join(order.icd10)
        }

        filename = f"ORDER_{uuid.uuid4().hex}.docx"

        path = os.path.join(OUTPUT_DIR, filename)

        template.render(context)
        template.save(path)

        validate_docx(path)

        files.append(filename)

    return files


@app.post("/generate")
def create_dme_documents(payload: Payload):

    vn_file = generate_vn(payload)

    order_files = generate_orders(payload)

    return {

        "vn_docx_url":
        f"{BASE_URL}/files/{vn_file}",

        "order_docx_urls":
        [
            f"{BASE_URL}/files/{f}"
            for f in order_files
        ],

        "success": True,
        "partial_failure": False,
        "message": "Documents generated"
    }


@app.get("/files/{filename}")
def get_file(filename: str):

    path = os.path.join(OUTPUT_DIR, filename)

    return FileResponse(path)
