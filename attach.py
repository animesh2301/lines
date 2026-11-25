import base64, requests

TENANT, CLIENT_ID, SECRET = "<tenant-id>", "<client-id>", "<client-secret>"
ENV, COMPANY, INVOICE_ID = "<environment>", "<company GUID>", "<draft invoice GUID>"
PDF = r"C:\path\to\test.pdf"

tok = requests.post(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token", data={
    "grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": SECRET,
    "scope": "https://api.businesscentral.dynamics.com/.default"}).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
base = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/{ENV}/api/v2.0/companies({COMPANY})"

pdf = open(PDF, "rb").read()
r = requests.post(f"{base}/documentAttachments", headers=h, json={
    "fileName": "single-post-test.pdf", "parentType": "Purchase_x0020_Invoice",
    "parentId": INVOICE_ID, "attachmentContent": base64.b64encode(pdf).decode()})
print("POST:", r.status_code, r.text[:300])

if r.ok:
    att = r.json()["id"]
    meta = requests.get(f"{base}/documentAttachments({att})", headers=h).json()
    body = requests.get(f"{base}/documentAttachments({att})/attachmentContent", headers=h).content
    print("file bytes:", len(pdf), "| byteSize:", meta.get("byteSize"), "| downloaded:", len(body))
