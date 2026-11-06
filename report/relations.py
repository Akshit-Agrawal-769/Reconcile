"""The relations and functional dependencies the report discusses, as data.

WORKED_A  the purchase register a CA keeps in Excel, taken from unnormalised form to BCNF
WORKED_B  a filing tracker where staff specialise by return type: 3NF but not BCNF,
          used to show what BCNF can cost (a dependency that is no longer preserved)
FINAL     the 18 tables of the XAMPP schema (books_staging, a load buffer, is left out)
"""
from dataclasses import dataclass, field

from report.fd import F, project


@dataclass
class Relation:
    attrs: frozenset
    fds: list
    derived: frozenset = frozenset()     # stored for an index, computed from other columns
    note: str = ""
    unique_not_keys: tuple = ()          # UNIQUE constraints over nullable columns


@dataclass
class Worked:
    attrs: frozenset
    fds: list
    stages: dict = field(default_factory=dict)   # stage -> {fragment name: attrs}


def A(s):
    return frozenset(s.split())


def projected(w, attrs):
    return project(w.fds, attrs)


def rel(attrs, *fds, derived="", note="", unique_not_keys=()):
    return Relation(A(attrs), [F(l, r) for l, r in fds], A(derived), note,
                    tuple(A(k) for k in unique_not_keys))


# ------------------------------------------------------------------ worked example A
# One row per invoice line, as exported from the client's accounting software and
# then annotated by the CA (the 2B tax is typed in next to each invoice).
WORKED_A = Worked(
    attrs=A("client_gstin client_pan client_name supplier_gstin supplier_name invoice_no "
            "invoice_date return_period line_no hsn rate taxable_value tax_amount portal_tax"),
    fds=[F("client_gstin", "client_pan"),
         F("client_pan", "client_name"),
         F("supplier_gstin", "supplier_name"),
         F("client_gstin supplier_gstin invoice_no", "invoice_date portal_tax"),
         F("invoice_date", "return_period"),
         F("client_gstin supplier_gstin invoice_no line_no", "hsn rate taxable_value tax_amount")],
    stages={
        "2NF": {
            "REGISTRATION": A("client_gstin client_pan client_name"),
            "SUPPLIER": A("supplier_gstin supplier_name"),
            "INVOICE_HDR": A("client_gstin supplier_gstin invoice_no invoice_date return_period portal_tax"),
            "INVOICE_LINE": A("client_gstin supplier_gstin invoice_no line_no hsn rate taxable_value tax_amount"),
        },
        "3NF": {
            "GSTIN_REG": A("client_gstin client_pan"),
            "CLIENT": A("client_pan client_name"),
            "SUPPLIER": A("supplier_gstin supplier_name"),
            "INVOICE": A("client_gstin supplier_gstin invoice_no invoice_date portal_tax"),
            "MONTH_OF": A("invoice_date return_period"),
            "INVOICE_LINE": A("client_gstin supplier_gstin invoice_no line_no hsn rate taxable_value tax_amount"),
        },
    },
)
WORKED_A.stages["BCNF"] = dict(WORKED_A.stages["3NF"])
# Column order as the text writes PURCHASE_LINE; the chase tableau letters it A to N.
WORKED_A_ORDER = ("client_gstin client_pan client_name supplier_gstin supplier_name invoice_no "
                  "invoice_date return_period line_no hsn rate taxable_value tax_amount portal_tax").split()   # already BCNF, shown in the text

# ------------------------------------------------------------------ worked example B
# A firm where GSTR-1 and GSTR-3B are prepared by different specialists. Each staff
# member handles one return type; each client has one person per return type.
WORKED_B = Worked(
    attrs=A("client_gstin return_type staff_email staff_phone"),
    fds=[F("client_gstin return_type", "staff_email"),
         F("staff_email", "return_type staff_phone")],
    stages={
        "3NF": {"STAFF": A("staff_email staff_phone"),
                "ASSIGNMENT": A("client_gstin return_type staff_email")},
        "BCNF": {"STAFF": A("staff_email return_type staff_phone"),
                 "CLIENT_STAFF": A("client_gstin staff_email")},
    },
)

# ------------------------------------------------------------------ the 18 final tables
FINAL = {
    "firm": rel("firm_id name", ("firm_id", "name")),
    "client": rel("client_id firm_id name pan approval_threshold rounding_tol date_window_days",
                  ("client_id", "firm_id name pan approval_threshold rounding_tol date_window_days"),
                  ("pan", "client_id")),
    "gstin_reg": rel("gstin client_id taxpayer_type reg_date",
                     ("gstin", "client_id taxpayer_type reg_date"),
                     note="state code and PAN are read off the GSTIN, not stored"),
    "party": rel("gstin name", ("gstin", "name")),
    "app_user": rel("user_id firm_id client_id email role password_hash",
                    ("user_id", "firm_id client_id email role password_hash"),
                    ("email", "user_id")),
    "staff_assignment": rel("user_id client_id", note="all-key relation"),
    "period_transition": rel("from_state to_state actor_role",
                             ("from_state to_state", "actor_role")),
    "return_period": rel("period_id gstin period state client_approved_at client_approved_by "
                         "override_reason closed_at",
                         ("period_id", "gstin period state client_approved_at client_approved_by "
                                       "override_reason closed_at"),
                         ("gstin period", "period_id")),
    "return_calendar": rel("return_type period due_date", ("return_type period", "due_date")),
    "filing": rel("gstin return_type period filed_on late_fee",
                  ("gstin return_type period", "filed_on late_fee")),
    "invoice": rel("invoice_id gstin party_gstin doc_type invoice_no invoice_date pos is_rcm imported_by",
                   ("invoice_id", "gstin party_gstin doc_type invoice_no invoice_date pos is_rcm imported_by"),
                   derived="invoice_no_norm fin_year",
                   note="invoice_no_norm = norm_doc_no(invoice_no), fin_year = FY of invoice_date"),
    "invoice_line": rel("invoice_id line_no hsn rate taxable_value igst cgst sgst cess itc_category",
                        ("invoice_id line_no", "hsn rate taxable_value igst cgst sgst cess itc_category")),
    "gstr2b_import": rel("import_id period_id version source imported_by imported_at",
                         ("import_id", "period_id version source imported_by imported_at"),
                         ("period_id version", "import_id")),
    "gstr2b_entry": rel("entry_id import_id party_gstin doc_type invoice_no invoice_date taxable_value "
                        "igst cgst sgst cess",
                        ("entry_id", "import_id party_gstin doc_type invoice_no invoice_date taxable_value "
                                     "igst cgst sgst cess"),
                        derived="invoice_no_norm fin_year",
                        note="a 2B may list the same document twice, so there is no second key"),
    "recon_result": rel("result_id import_id invoice_id entry_id status diff_tax",
                        ("result_id", "import_id invoice_id entry_id status diff_tax"),
                        unique_not_keys=("import_id invoice_id", "import_id entry_id"),
                        note="(import_id, invoice_id) and (import_id, entry_id) are UNIQUE but contain "
                             "NULLs, so they are not candidate keys"),
    "review": rel("review_id gstin party_gstin doc_type invoice_no_norm fin_year decision ims_action "
                  "claim_pct remark state proposed_by approved_by executed_on",
                  ("review_id", "gstin party_gstin doc_type invoice_no_norm fin_year decision ims_action "
                                "claim_pct remark state proposed_by approved_by executed_on"),
                  ("gstin party_gstin doc_type invoice_no_norm fin_year", "review_id")),
    "followup": rel("followup_id period_id party_gstin channel sent_by sent_on response next_chase_on status",
                    ("followup_id", "period_id party_gstin channel sent_by sent_on response next_chase_on status")),
    "audit_log": rel("audit_id table_name row_key action changed_by changed_at detail",
                     ("audit_id", "table_name row_key action changed_by changed_at detail")),
}
