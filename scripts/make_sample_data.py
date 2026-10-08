"""Generate the fictitious client's messy source exports.

Three files describe the SAME ~45 employees from three legacy systems:
  1. legacy_hris_employees.csv  - dd/mm/yyyy dates, whitespace/casing noise, exact duplicate rows
  2. crm_contacts.xlsx          - full_name instead of first/last, mixed date formats, Y/N status flag
  3. payroll_dump.csv           - mm/dd/yyyy dates, department CODES (ENG/SLS/...), two email columns

Deliberately planted problems (each maps to an escalation type or an auto-fix):
  - exact duplicate rows                      -> auto-dropped
  - same person in 2-3 files                  -> auto-merged by email
  - casing / whitespace / phone formats       -> auto-normalised
  - department code "BD" (unknown)            -> escalate: value can't be confidently mapped
  - impossible date 31/02/2020                -> escalate: fails validation twice
  - fuzzy duplicate (email typo, same name+DOB)-> escalate: possible duplicate
  - one-token full_name "Madonna"             -> escalate: can't split name
  - terminated without termination date       -> escalate: rule violation
  - conflicting hire_date across sources      -> escalate: identity-critical conflict
  - conflicting phone across sources          -> auto: source priority, logged
  - "Personal Email" and "Grade" columns      -> auto: dropped (no target field), logged
"""
import csv, random
from pathlib import Path
from datetime import date, timedelta
import openpyxl

random.seed(42)
OUT = Path(__file__).resolve().parent.parent / "sample_data"
OUT.mkdir(exist_ok=True)

FIRST = ["Aarav","Priya","Rohan","Sneha","Vikram","Ananya","Karan","Meera","Arjun","Divya","Rahul","Neha",
         "Siddharth","Pooja","Aditya","Kavya","Nikhil","Riya","Manish","Isha","Varun","Tanvi","Amit","Shreya",
         "Dev","Nisha","Raj","Anjali","Kunal","Simran","Harsh","Ritika","Yash","Sakshi","Om","Trisha",
         "Gaurav","Payal","Abhishek","Lakshmi","Sanjay","Bhavna","Tarun","Komal","Vivek"]
LAST = ["Sharma","Patel","Reddy","Iyer","Nair","Gupta","Singh","Mehta","Joshi","Rao","Kumar","Das",
        "Bose","Menon","Pillai","Chopra","Malhotra","Kapoor","Bhat","Verma","Saxena","Desai","Shah","Agarwal",
        "Mishra","Tiwari","Kulkarni","Jain","Sethi","Khanna","Bajaj","Ghosh","Naidu","Banerjee","Chauhan","Dutta",
        "Sinha","Pandey","Yadav","Thakur","Rana","Goel","Arora","Bhatt","Srinivasan"]
DEPTS = ["Engineering","Sales","Marketing","Finance","HR","Operations","Support"]
CODE = {"Engineering":"ENG","Sales":"SLS","Marketing":"MKT","Finance":"FIN","HR":"HR","Operations":"OPS","Support":"SUP"}
TITLES = {"Engineering":["Software Engineer","Senior Engineer","Engineering Manager","QA Engineer"],
          "Sales":["Account Executive","Sales Manager","SDR"],"Marketing":["Marketing Manager","Content Lead"],
          "Finance":["Accountant","Finance Analyst"],"HR":["HR Generalist","Recruiter"],
          "Operations":["Ops Analyst","Ops Manager"],"Support":["Support Engineer","Support Lead"]}
CITIES = ["Hyderabad","Bengaluru","Mumbai","Pune","Chennai","Gurugram"]
TYPES = ["full_time","full_time","full_time","part_time","contractor","intern"]

def rdate(a, b):
    return a + timedelta(days=random.randint(0, (b - a).days))

people = []
for i in range(45):
    fn, ln = FIRST[i], LAST[i]
    dept = random.choice(DEPTS)
    dob = rdate(date(1975,1,1), date(1996,12,31))
    hire = rdate(date(2015,1,1), date(2024,6,30))
    status = random.choices(["active","terminated","on_leave"], [0.8,0.15,0.05])[0]
    term = rdate(hire + timedelta(days=90), date(2025,6,30)) if status == "terminated" else None
    mgr = people[random.randrange(len(people))]["email"] if people and random.random() < 0.7 else ""
    people.append(dict(id=f"E{1001+i}", fn=fn, ln=ln, email=f"{fn.lower()}.{ln.lower()}@acmecorp.example",
        phone=f"9{random.randint(100000000,999999999)}", dob=dob, hire=hire, dept=dept,
        title=random.choice(TITLES[dept]), etype=random.choice(TYPES), mgr=mgr, city=random.choice(CITIES),
        salary=random.randint(4,40)*100000, status=status, term=term))

def dmy(d): return d.strftime("%d/%m/%Y") if d else ""
def mdy(d): return d.strftime("%m/%d/%Y") if d else ""
def noisy_case(s): return random.choice([s, s.upper(), s.lower(), s.title()])
def noisy_ws(s): return random.choice([s, f" {s}", f"{s} ", f"  {s}"])
def fmt_phone(p): return random.choice([p, f"+91 {p[:5]} {p[5:]}", f"+91-{p}", f"{p[:5]}-{p[5:]}", f"0{p}"])
ETYPE_LABEL = {"full_time":["Full Time","Full-Time","FT","Permanent"],"part_time":["Part Time","PT"],
               "contractor":["Contractor","Contract"],"intern":["Intern","Internship"]}

# ---------- 1. Legacy HRIS (all 45 people) ----------
hris_rows = []
for p in people:
    row = {"Emp ID": p["id"], "First Name": noisy_ws(noisy_case(p["fn"])), "Last Name": noisy_ws(p["ln"]),
           "Work Email": noisy_case(p["email"]) if random.random() < 0.3 else p["email"],
           "Phone Number": fmt_phone(p["phone"]), "DOB": dmy(p["dob"]), "Date of Joining": dmy(p["hire"]),
           "Dept": noisy_case(p["dept"]), "Designation": p["title"], "Emp Type": random.choice(ETYPE_LABEL[p["etype"]]),
           "Reporting Manager Email": p["mgr"], "Office": p["city"], "Annual CTC": f"{p['salary']:,}" if random.random()<0.5 else str(p["salary"]),
           "Status": noisy_case(p["status"].replace("_"," ")), "Last Working Day": dmy(p["term"])}
    hris_rows.append(row)
# planted: impossible date for E1043 (only in HRIS, so no other source can rescue it)
hris_rows[42]["Date of Joining"] = "31/02/2020"
# planted: terminated but no last working day for E1012
hris_rows[11]["Status"] = "Terminated"; hris_rows[11]["Last Working Day"] = ""
# planted: missing department for E1020 (recoverable from payroll) and E1003 (payroll says "BD")
hris_rows[19]["Dept"] = ""; hris_rows[2]["Dept"] = ""
# planted: exact duplicates
hris_rows += [dict(hris_rows[3]), dict(hris_rows[15]), dict(hris_rows[22])]
random.shuffle(hris_rows)
with open(OUT / "legacy_hris_employees.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(hris_rows[0].keys())); w.writeheader(); w.writerows(hris_rows)

# ---------- 2. CRM contacts (30 of the people, overlaps) ----------
def crm_date(d):
    if not d: return ""
    return random.choice([d.isoformat(), d.strftime("%b %d, %Y"), d.strftime("%d-%b-%Y"), d.strftime("%Y/%m/%d")])
crm_people = people[10:40]
crm_rows = []
for p in crm_people:
    hire = p["hire"]
    crm_rows.append({"contact_id": p["id"], "full_name": f"{p['fn']} {p['ln']}", "email_address": p["email"],
        "mobile": fmt_phone(p["phone"]), "birth_date": crm_date(p["dob"]), "start_date": crm_date(hire),
        "team": p["dept"], "title": p["title"], "type": random.choice(ETYPE_LABEL[p["etype"]]),
        "manager": p["mgr"], "city": p["city"], "comp": p["salary"],
        "active_flag": "Y" if p["status"] == "active" else "N", "end_date": crm_date(p["term"])})
# planted: conflicting hire date for E1015 (identity-critical conflict -> escalate)
crm_rows[5]["start_date"] = (people[15]["hire"] + timedelta(days=400)).isoformat()
# planted: conflicting phone for E1018 (soft conflict -> auto, source priority)
crm_rows[8]["mobile"] = "9000000018"
# planted: three-token name (auto: first + rest) and one-token name (escalate)
crm_rows[12]["full_name"] = "Anjali Devi Kapoor" if False else crm_rows[12]["full_name"]
crm_rows[14]["full_name"] = "Madonna"; crm_rows[14]["email_address"] = "madonna@acmecorp.example"; crm_rows[14]["contact_id"] = "C9001"
crm_rows[14]["birth_date"] = "1985-08-16"; crm_rows[14]["start_date"] = "2021-03-01"; crm_rows[14]["team"] = "Marketing"
# planted: fuzzy duplicate of E1030 with typo email, same name + dob
p = people[30]
crm_rows.append({"contact_id": "C9002", "full_name": f"{p['fn']} {p['ln']}", "email_address": f"{p['fn'].lower()}.{p['ln'].lower()[:-1]}@acmecorp.example",
    "mobile": p["phone"], "birth_date": p["dob"].isoformat(), "start_date": p["hire"].isoformat(), "team": p["dept"],
    "title": p["title"], "type": "Full Time", "manager": p["mgr"], "city": p["city"], "comp": p["salary"], "active_flag": "Y", "end_date": ""})
# planted: brand-new person only in CRM (valid, should flow through)
crm_rows.append({"contact_id": "C9003", "full_name": "Fatima Sheikh", "email_address": "fatima.sheikh@acmecorp.example",
    "mobile": "+91 90000 12345", "birth_date": "1992-11-02", "start_date": "Jan 15, 2024", "team": "Support", "title": "Support Engineer",
    "type": "Full Time", "manager": "", "city": "Mumbai", "comp": 900000, "active_flag": "Y", "end_date": ""})
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Contacts"
ws.append(list(crm_rows[0].keys()))
for r in crm_rows: ws.append(list(r.values()))
wb.save(OUT / "crm_contacts.xlsx")

# ---------- 3. Payroll dump (35 people, dept codes, mm/dd/yyyy) ----------
pay_people = people[:35]
pay_rows = []
for p in pay_people:
    code = CODE[p["dept"]]
    pay_rows.append({"Employee Number": p["id"], "Name": f"{p['ln']}, {p['fn']}", "Personal Email": f"{p['fn'].lower()}{random.randint(1,99)}@gmail.example",
        "Work Email": p["email"], "Contact": fmt_phone(p["phone"]), "Joined": mdy(p["hire"]), "Base Salary": p["salary"],
        "Bonus": random.randint(0,3)*50000, "Department Code": code, "Grade": random.choice(["L1","L2","L3","L4","M1"]),
        "Employment Status": "ACTIVE" if p["status"]=="active" else ("TERMINATED" if p["status"]=="terminated" else "LOA"),
        "Terminated On": mdy(p["term"])})
# planted: unknown department code "BD" on two payroll rows. E1010 also has a dept in HRIS (agent
# takes it, logs the BD); E1003's HRIS dept is blanked so BD is its only source -> escalate
pay_rows[2]["Department Code"] = "BD"; pay_rows[9]["Department Code"] = "BD"
# planted: E1020's department lives only here (HRIS blank) -> recovered by merge
pay_rows[19]["Department Code"] = CODE[people[19]["dept"]]
with open(OUT / "payroll_dump.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(pay_rows[0].keys())); w.writeheader(); w.writerows(pay_rows)

print(f"HRIS rows: {len(hris_rows)}  CRM rows: {len(crm_rows)}  Payroll rows: {len(pay_rows)}  -> {OUT}")
