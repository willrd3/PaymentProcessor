"""
Lambda: document_processor.py
Demo ingest Lambda that accepts an API Gateway proxy event containing JSON with keys:
- correlationId
- userId
- fileName
- documentBase64
- documentType

Environment variables expected:
- OPENAI_API_KEY
- AWS_REGION (optional, default us-east-1)
- RESULTS_CALLBACK_URL (optional)

This version logs processing results to CloudWatch (Lambda logs) instead of writing to DynamoDB.
Dependencies: requests, pdfplumber(optional), openai(optional)

This file is intended for local editing and deployment to AWS Lambda (package deps accordingly).
"""

import os
import json
import base64
import io
import time
import traceback
import logging

# Optional: pdfplumber for text extraction for text PDFs
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# Optional: OpenAI SDK
try:
    import openai
except Exception:
    openai = None

import requests

# Configure logger for CloudWatch
logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig()
logger.setLevel(logging.INFO)

# Configuration from environment
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
RESULTS_CALLBACK_URL = os.environ.get("RESULTS_CALLBACK_URL")

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY


def is_valid_routing_number(rn: str) -> bool:
    """ABA routing checksum validation (9 digits)"""
    if not rn or len(rn) != 9 or not rn.isdigit():
        return False
    digits = list(map(int, rn))
    checksum = (3 * (digits[0] + digits[3] + digits[6])
                + 7 * (digits[1] + digits[4] + digits[7])
                + 1 * (digits[2] + digits[5] + digits[8]))
    return checksum % 10 == 0


def call_openai_ocr_pdf(pdf_bytes: bytes) -> str:
    """Fallback OCR using OpenAI: send a truncated base64 PDF and ask the model to extract text.
    This requires a vision-capable model or may work heuristically. Results may vary.
    """
    if not openai or not OPENAI_API_KEY:
        return ""
    try:
        # Encode and truncate to avoid enormous payloads; keep within token limits.
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        max_chars = 200000  # conservative
        if len(b64) > max_chars:
            b64_snippet = b64[:max_chars]
            note = f"(truncated base64, showing first {max_chars} chars)\n"
        else:
            b64_snippet = b64
            note = ""

        prompt = (
            "You are a PDF OCR assistant. A PDF file has been encoded in base64.\n"
            "Attempt to extract any human-readable text from the PDF. If you cannot decode the PDF, try to infer textual content from the encoded bytes.\n"
            "Return ONLY the extracted text, with no additional commentary.\n\n"
            "Base64 PDF (may be truncated):\n"
            + note + b64_snippet
        )

        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        txt = resp["choices"][0]["message"]["content"]
        return txt
    except Exception:
        logger.exception("OpenAI OCR fallback failed")
        return ""


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Try to extract text using pdfplumber; if unavailable or empty, try OpenAI OCR fallback."""
    text = ""
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n".join(pages)
        except Exception:
            logger.exception("pdfplumber text extraction failed")
            text = ""

    # If pdfplumber is not available or extraction returned empty, try OpenAI OCR fallback
    if not text and openai and OPENAI_API_KEY:
        logger.info("Falling back to OpenAI OCR for PDF text extraction")
        try:
            text = call_openai_ocr_pdf(pdf_bytes)
        except Exception:
            logger.exception("OpenAI OCR call failed")
            text = ""

    return text


def detect_biller(text: str) -> str:
    patterns = {
        "AT&T": ["AT&T", "ATT", "att.com"],
        "Xfinity": ["Xfinity", "Comcast", "xfinity.com"],
        "City Utilities": ["Utility Billing", "City of", "Water Bill", "Electric Bill"]
    }
    if not text:
        return "Unknown"
    lower = text.lower()
    for biller, kws in patterns.items():
        for kw in kws:
            if kw.lower() in lower:
                return biller
    return "Unknown"


def call_openai_extract_fields(document_text: str) -> dict:
    """Call OpenAI to extract invoice fields as JSON. If OpenAI not configured, return empty fields."""
    if not openai or not OPENAI_API_KEY or not document_text:
        return {"invoiceNumber": None, "amount": None, "dueDateRaw": None, "routingNumber": None, "accountNumber": None, "payeeName": None}

    prompt = (
        "You are a JSON extractor for invoice/payment PDFs.\n"
        "Extract fields: invoiceNumber, amount, dueDateRaw, routingNumber, accountNumber, payeeName.\n"
        "Return only JSON with these keys, use null for missing values.\n\n"
        "Document text:\n\"\"\"" + document_text[:5000] + "\"\"\""
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You extract invoice fields as JSON."}, {"role": "user", "content": prompt}],
            temperature=0
        )
        txt = resp["choices"][0]["message"]["content"]
        return json.loads(txt)
    except Exception:
        logger.exception("OpenAI extraction failed")
        # fallback: no AI
        return {"invoiceNumber": None, "amount": None, "dueDateRaw": None, "routingNumber": None, "accountNumber": None, "payeeName": None}


def normalize_due_date_via_openai(due_date_raw: str) -> dict:
    if not openai or not OPENAI_API_KEY or not due_date_raw:
        return {"normalized": None, "note": "ai-not-configured"}
    prompt = (
        "Normalize this invoice due date text into a single ISO date (YYYY-MM-DD). If ambiguous, return JSON with normalized=null and note explaining ambiguity.\n"
        f"Text: '{due_date_raw}'\nReturn JSON: {{\"normalized\":...,\"note\":...}}"
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        return json.loads(resp["choices"][0]["message"]["content"])
    except Exception:
        logger.exception("OpenAI date normalization failed")
        return {"normalized": None, "note": "ai-failed"}


def lambda_handler(event, context):
    start = time.time()
    try:
        # API Gateway proxy: body is a JSON string
        body_raw = event.get("body") if isinstance(event, dict) else event
        try:
            body = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})
        except Exception:
            body = body_raw or {}

        correlation_id = body.get("correlationId") or f"cid-{int(time.time()*1000)}"
        document_b64 = body.get("documentBase64")
        user_id = body.get("userId", "demo")
        file_name = body.get("fileName", "uploaded.pdf")

        if not document_b64:
            return {"statusCode": 400, "body": json.dumps({"error": "documentBase64 required"})}

        try:
            pdf_bytes = base64.b64decode(document_b64)
        except Exception:
            return {"statusCode": 400, "body": json.dumps({"error": "invalid base64"})}

        text = extract_text_from_pdf_bytes(pdf_bytes)
        biller = detect_biller(text)

        extracted = call_openai_extract_fields(text)

        errors = []
        ai_suggestions = {}

        # routing validation
        routing = extracted.get("routingNumber")
        if routing:
            if not is_valid_routing_number(routing):
                # no correction logic here, just flag
                errors.append({"field": "routingNumber", "reason": "Invalid ABA checksum", "suggestedFix": None, "confidence": 0.6})

        # normalize due date
        due_raw = extracted.get("dueDateRaw")
        if due_raw:
            norm = normalize_due_date_via_openai(due_raw)
            normalized = norm.get("normalized")
            if normalized:
                extracted["dueDate"] = normalized
            else:
                errors.append({"field": "dueDate", "reason": "Ambiguous date format", "suggestedFix": norm.get("note"), "confidence": 0.5})

        status = "needs_review" if errors else "approved"

        item = {
            "correlationId": correlation_id,
            "userId": user_id,
            "fileName": file_name,
            "status": status,
            "extracted": extracted,
            "errors": errors,
            "aiSuggestions": ai_suggestions,
            "billerDetected": biller,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "processingTimeMs": int((time.time() - start) * 1000)
        }

        # Log the full result to CloudWatch logs
        try:
            logger.info("Document processed: %s", json.dumps(item))
        except Exception:
            logger.exception("Failed to log item to CloudWatch")

        # optional callback to your app
        if RESULTS_CALLBACK_URL:
            try:
                requests.post(RESULTS_CALLBACK_URL, json={"correlationId": correlation_id}, timeout=3)
            except Exception:
                logger.exception("Callback POST failed")

        return {"statusCode": 200, "body": json.dumps({"correlationId": correlation_id, "status": status})}

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("Lambda failure: %s", str(e))
        return {"statusCode": 500, "body": json.dumps({"error": "lambda-failed", "detail": str(e)})}
