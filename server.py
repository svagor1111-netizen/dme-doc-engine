from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from docxtpl import DocxTemplate
import uuid
import os
import zipfile

app = FastAPI()

BASE_URL = "https://dme-doc-engine.onrender.com"
OUTPUT_DIR = "output"
TEMPLATES_DIR = "templates"

os.makedirs(OUTPUT_DIR, exist_ok=True)

DEFAULT_PHYSICIAN_NAME = os.getenv("DEFAULT_PHYSICIAN_NAME", "")
DEFAULT_PRACTICE_NAME = os.getenv("DEFAULT_PRACTICE_NAME", "")
DEFAULT_PRACTICE_ADDRESS = os.getenv("DEFAULT_PRACTICE_ADDRESS", "")
DEFAULT_PRACTICE_PHONE = os.getenv("DEFAULT_PRACTICE_PHONE", "")
DEFAULT_PRACTICE_FAX = os.getenv("DEFAULT_PRACTICE_FAX", "")
DEFAULT_NPI = os.getenv("DEFAULT_NPI", "")

# Blocked in the main VNM generator
BLOCKED_EQUIPMENT_TERMS = {
    "underpads",
    "under pads",
    "under pad",
    "underpads / chux",
    "under pads / chux",
    "chux",
    "chucks",
}


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


class EquipmentDetail(BaseModel):
    name: str
    dx: str
    medical_necessity: str


class Payload(BaseModel):
    patient_name: str
    dob: str
    age: str
    sex: str

    physician_name: str
    practice_address: str
    practice_phone: str
    practice_fax: str
    practice_name: Optional[str] = ""
    npi: Optional[str] = ""

    facility_name: str
    facility_address: str
    facility_phone: str

    exam_date: str
    signature_date: str

    vitals: Vitals
    diagnoses: List[Diagnosis]
    equipment_list: List[str]

    vn_text: Optional[str] = ""
    orders: Optional[List[OrderItem]] = []

    primary_diagnosis: Optional[str] = ""
    secondary_diagnoses: Optional[str] = ""
    functional_status: Optional[str] = ""
    cognitive_status: Optional[str] = ""
    ambulatory_status: Optional[str] = ""
    general_health_status: Optional[str] = ""

    equipment_details: Optional[List[EquipmentDetail]] = []

    # Raw VN equipment blocks sent by GPT.
    # These match MASTER_VN.docx placeholders directly and must be preserved.
    equipment_1: Optional[str] = ""
    equipment_2: Optional[str] = ""
    equipment_3: Optional[str] = ""
    equipment_4: Optional[str] = ""
    equipment_5: Optional[str] = ""
    equipment_6: Optional[str] = ""
    equipment_7: Optional[str] = ""
    equipment_8: Optional[str] = ""

    mode: Optional[str] = "vnm"


def first_non_empty(*values: Optional[str]) -> str:
    for value in values:
        if value is not None:
            s = str(value).strip()
            if s:
                return s
    return ""


def normalize_equipment_name(name: str) -> str:
    return " ".join((name or "").strip().lower().replace("-", " ").replace("/", " / ").split())


def is_blocked_equipment(name: str) -> bool:
    normalized = normalize_equipment_name(name)
    if normalized in BLOCKED_EQUIPMENT_TERMS:
        return True

    # broader safe catch for common variants
    if "underpad" in normalized or normalized == "chux" or " / chux" in normalized:
        return True

    return False


def all_icd_codes(payload: Payload) -> str:
    if not payload.diagnoses:
        return ""
    return ", ".join([d.code for d in payload.diagnoses if d.code])


def normalized_signature_date(payload: Payload) -> str:
    return first_non_empty(payload.signature_date, payload.exam_date)


def validate_docx(path: str) -> None:
    if not os.path.exists(path):
        raise Exception(f"File not found: {path}")

    if not zipfile.is_zipfile(path):
        raise Exception("Invalid DOCX structure")

    with zipfile.ZipFile(path, "r") as z:
        names = z.namelist()
        required = [
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
        ]
        for item in required:
            if item not in names:
                raise Exception(f"Corrupted DOCX: missing {item}")


def ensure_templates_exist() -> None:
    vn_template = os.path.join(TEMPLATES_DIR, "MASTER_VN.docx")
    order_template = os.path.join(TEMPLATES_DIR, "MASTER_ORDER.docx")
    inc_template = os.path.join(TEMPLATES_DIR, "MASTER_INCONTINENCE.docx")

    if not os.path.exists(vn_template):
        raise Exception("Missing template: templates/MASTER_VN.docx")

    if not os.path.exists(order_template):
        raise Exception("Missing template: templates/MASTER_ORDER.docx")

    if not os.path.exists(inc_template):
        raise Exception("Missing template: templates/MASTER_INCONTINENCE.docx")


def ensure_code_first(text: str, code_prefix: str) -> str:
    clean_text = (text or "").strip()
    clean_code = (code_prefix or "").strip()

    if not clean_text:
        return ""

    if not clean_code:
        return clean_text

    if clean_text.startswith(clean_code):
        return clean_text

    return f"{clean_code} – {clean_text}"


def default_primary_dx(payload: Payload) -> str:
    primary_code = payload.diagnoses[0].code if payload.diagnoses else ""

    if payload.primary_diagnosis and payload.primary_diagnosis.strip():
        return ensure_code_first(payload.primary_diagnosis.strip(), primary_code)

    if payload.diagnoses:
        d = payload.diagnoses[0]
        return f"{d.code} – {d.label}"

    return ""


def default_secondary_dx(payload: Payload) -> str:
    # IMPORTANT:
    # Do NOT prepend comma-separated ICD prefixes here.
    # Old behavior created: "I11.9, B20, R56.9 – I11.9 ..."
    if payload.secondary_diagnoses and payload.secondary_diagnoses.strip():
        return payload.secondary_diagnoses.strip()

    if len(payload.diagnoses) > 1:
        return "\n".join([f"{d.code} – {d.label}" for d in payload.diagnoses[1:]])

    return ""


def filtered_equipment_entries(payload: Payload) -> List[tuple[str, str, str]]:
    """
    Produces effective VN equipment entries while removing blocked items.
    Fallback only: prefer equipment_details, fall back to equipment_list.
    """
    entries: List[tuple[str, str, str]] = []
    equipment_details = payload.equipment_details or []
    equipment_list = payload.equipment_list or []
    icd_string = all_icd_codes(payload)

    total_items = min(8, max(len(equipment_details), len(equipment_list)))

    for i in range(total_items):
        if i < len(equipment_details):
            item = equipment_details[i]
            name = (item.name or "").strip()
            dx = (item.dx or icd_string).strip()
            medical_necessity = (item.medical_necessity or "").strip()
        else:
            name = (equipment_list[i] if i < len(equipment_list) else "").strip()
            dx = icd_string
            medical_necessity = ""

        if not name:
            continue

        if is_blocked_equipment(name):
            continue

        entries.append((name, dx, medical_necessity))

        if len(entries) >= 8:
            break

    return entries


def build_vn_equipment_fields(payload: Payload) -> dict:
    fields = [""] * 8

    # First priority: raw equipment_1..equipment_8 blocks sent by GPT.
    # These already contain the final VN wording:
    # 1. Equipment
    # Relevant Dx/ICD-10
    # Medical Necessity
    raw_fields = [
        payload.equipment_1,
        payload.equipment_2,
        payload.equipment_3,
        payload.equipment_4,
        payload.equipment_5,
        payload.equipment_6,
        payload.equipment_7,
        payload.equipment_8,
    ]

    has_raw_fields = any((value or "").strip() for value in raw_fields)

    if has_raw_fields:
        for idx, value in enumerate(raw_fields):
            text = (value or "").strip()
            if not text:
                continue

            # Skip blocked supply items if they appear in the raw block heading.
            first_line = text.splitlines()[0] if text.splitlines() else text
            if is_blocked_equipment(first_line):
                continue

            fields[idx] = text
    else:
        # Backward-compatible fallback:
        # equipment_details may contain medical necessity; equipment_list alone does not.
        entries = filtered_equipment_entries(payload)

        for idx, (name, dx, medical_necessity) in enumerate(entries):
            fields[idx] = (
                f"{idx + 1}. {name}\n"
                f"Relevant Dx/ICD-10: {dx}\n"
                f"Medical Necessity: {medical_necessity}"
            )

    return {
        "equipment_1": fields[0],
        "equipment_2": fields[1],
        "equipment_3": fields[2],
        "equipment_4": fields[3],
        "equipment_5": fields[4],
        "equipment_6": fields[5],
        "equipment_7": fields[6],
        "equipment_8": fields[7],
    }


def build_vn_context(payload: Payload) -> dict:
    vn_equipment_fields = build_vn_equipment_fields(payload)

    context = {
        "physician_name": first_non_empty(payload.physician_name, DEFAULT_PHYSICIAN_NAME),
        "practice_name": first_non_empty(payload.practice_name, DEFAULT_PRACTICE_NAME),
        "practice_address": first_non_empty(
            payload.practice_address,
            DEFAULT_PRACTICE_ADDRESS
        ),
        "practice_phone": first_non_empty(payload.practice_phone, DEFAULT_PRACTICE_PHONE),
        "practice_fax": first_non_empty(payload.practice_fax, DEFAULT_PRACTICE_FAX),
        "exam_date": first_non_empty(payload.exam_date, normalized_signature_date(payload)),

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

        "primary_diagnosis": default_primary_dx(payload),
        "secondary_diagnoses": default_secondary_dx(payload),

        "functional_status": payload.functional_status or "",
        "cognitive_status": payload.cognitive_status or "",
        "ambulatory_status": payload.ambulatory_status or "",
        "general_health_status": payload.general_health_status or "",

        "signature_date": normalized_signature_date(payload),

        **vn_equipment_fields,
    }

    return context


def build_order_context(payload: Payload, order: OrderItem) -> dict:
    diagnosis_text = payload.primary_diagnosis or ", ".join(
        [f"{d.code} – {d.label}" for d in payload.diagnoses]
    )

    icd_codes = ", ".join(order.icd10) if order.icd10 else all_icd_codes(payload)

    equipment_1 = order.items[0] if len(order.items) > 0 else ""
    equipment_2 = order.items[1] if len(order.items) > 1 else ""

    return {
        "physician_name": first_non_empty(payload.physician_name, DEFAULT_PHYSICIAN_NAME),
        "practice_name": first_non_empty(payload.practice_name, DEFAULT_PRACTICE_NAME),
        "practice_address": first_non_empty(
            payload.practice_address,
            DEFAULT_PRACTICE_ADDRESS
        ),
        "practice_phone": first_non_empty(payload.practice_phone, DEFAULT_PRACTICE_PHONE),
        "practice_fax": first_non_empty(payload.practice_fax, DEFAULT_PRACTICE_FAX),
        "patient_name": payload.patient_name,
        "equipment_1_name": equipment_1,
        "equipment_2_name": equipment_2,
        "diagnosis_text": diagnosis_text,
        "icd_codes": icd_codes,
        "signature_date": normalized_signature_date(payload),
        "npi": first_non_empty(payload.npi, DEFAULT_NPI, "1295174860"),
    }


def build_incontinence_context(payload: Payload) -> dict:
    primary_icd = payload.diagnoses[0].code if payload.diagnoses else ""
    secondary_icd = ", ".join([d.code for d in payload.diagnoses[1:]]) if len(payload.diagnoses) > 1 else ""

    full_address = first_non_empty(
        payload.practice_address,
        DEFAULT_PRACTICE_ADDRESS
    )

    city = ""
    state = ""
    zip_code = ""

    parts = [p.strip() for p in full_address.split(",")]
    if len(parts) >= 3:
        city = parts[-2]
        state_zip = parts[-1].split()
        if len(state_zip) >= 2:
            state = state_zip[0]
            zip_code = state_zip[1]

    return {
        "patient_name": payload.patient_name,
        "dob": payload.dob,
        "height": payload.vitals.height,
        "weight": payload.vitals.weight,
        "primary_icd": primary_icd,
        "secondary_icd": secondary_icd,
        "physician_name": first_non_empty(payload.physician_name, DEFAULT_PHYSICIAN_NAME),
        "practice_address": full_address,
        "practice_phone": first_non_empty(payload.practice_phone, DEFAULT_PRACTICE_PHONE),
        "practice_fax": first_non_empty(payload.practice_fax, DEFAULT_PRACTICE_FAX),
        "npi": first_non_empty(payload.npi, DEFAULT_NPI, "1295174860"),
        "city": city,
        "state": state,
        "zip": zip_code,
        "signature_date": normalized_signature_date(payload),
    }


def generate_vn(payload: Payload) -> str:
    template_path = os.path.join(TEMPLATES_DIR, "MASTER_VN.docx")
    template = DocxTemplate(template_path)

    context = build_vn_context(payload)

    filename = f"VN_{uuid.uuid4().hex}.docx"
    path = os.path.join(OUTPUT_DIR, filename)

    template.render(context)
    template.save(path)
    validate_docx(path)

    return filename


def split_orders_if_needed(payload: Payload) -> List[OrderItem]:
    fixed_orders: List[OrderItem] = []

    for order in payload.orders or []:
        filtered_items = [
            item for item in (order.items or [])
            if item and not is_blocked_equipment(item)
        ]
        icd10 = order.icd10 or []

        if not filtered_items:
            continue

        if len(filtered_items) <= 2:
            fixed_orders.append(OrderItem(items=filtered_items, icd10=icd10))
        else:
            for i in range(0, len(filtered_items), 2):
                chunk = filtered_items[i:i + 2]
                if chunk:
                    fixed_orders.append(OrderItem(items=chunk, icd10=icd10))

    return fixed_orders


def generate_orders(payload: Payload) -> List[str]:
    template_path = os.path.join(TEMPLATES_DIR, "MASTER_ORDER.docx")
    files: List[str] = []

    normalized_orders = split_orders_if_needed(payload)

    for order in normalized_orders:
        template = DocxTemplate(template_path)
        context = build_order_context(payload, order)

        filename = f"ORDER_{uuid.uuid4().hex}.docx"
        path = os.path.join(OUTPUT_DIR, filename)

        template.render(context)
        template.save(path)
        validate_docx(path)

        files.append(filename)

    return files


def generate_incontinence(payload: Payload) -> str:
    template_path = os.path.join(TEMPLATES_DIR, "MASTER_INCONTINENCE.docx")
    template = DocxTemplate(template_path)

    context = build_incontinence_context(payload)

    filename = f"ORDER_{uuid.uuid4().hex}.docx"
    path = os.path.join(OUTPUT_DIR, filename)

    template.render(context)
    template.save(path)
    validate_docx(path)

    return filename


@app.get("/")
def root():
    return {"ok": True, "service": "dme-doc-engine"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/version")
def version():
    return {
        "version": "equipment-fields-fix-2026-05-06-ready",
        "equipment_fields_enabled": True,
        "raw_equipment_blocks": True,
        "secondary_prefix_removed": True,
    }


@app.post("/create_dme_documents")
def create_dme_documents(payload: Payload):
    try:
        ensure_templates_exist()

        vn_file = generate_vn(payload)

        if payload.mode == "vnmi":
            inc_file = generate_incontinence(payload)

            return {
                "vn_docx_url": f"{BASE_URL}/files/{vn_file}",
                "order_docx_urls": [f"{BASE_URL}/files/{inc_file}"],
                "success": True,
                "partial_failure": False,
                "message": "VN + Orders generated"
            }

        if not payload.orders:
            raise HTTPException(status_code=400, detail="orders are missing")

        order_files = generate_orders(payload)

        return {
            "vn_docx_url": f"{BASE_URL}/files/{vn_file}",
            "order_docx_urls": [f"{BASE_URL}/files/{f}" for f in order_files],
            "success": True,
            "partial_failure": False,
            "message": "VN + Orders generated"
        }

    except HTTPException:
        raise
    except Exception as e:
        return {
            "vn_docx_url": "",
            "order_docx_urls": [],
            "success": False,
            "partial_failure": False,
            "message": str(e)
        }


@app.get("/files/{filename}")
def get_file(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
