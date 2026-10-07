"""CSV exports required by the assignment: dataset, messages, outreach tracker."""
import csv
import json
from pathlib import Path

NA = "Not Available"


def _write(path: str, header: list[str], rows: list[list]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_all(store, out_dir: str = "data") -> dict:
    d = Path(out_dir)
    infs = store.influencers()
    _write(d / "influencer_dataset.csv",
           ["Name", "Platform", "Followers", "Engagement (%)", "Niche", "Email", "Profile URL", "Content Themes",
            "Status", "Filter Result / Reasons", "Instagram", "Website", "Country", "Audience Age",
            "Audience Gender", "Audience Geography", "Email Source", "Data Source"],
           [[i["name"], i["platform"], i["followers"] if i["followers"] is not None else NA,
             i["engagement_rate"] if i["engagement_rate"] is not None else NA,
             i["niche"] or NA, i["email"] or ("Not Found" if i["status"] == "shortlisted" else NA),
             i["profile_url"], ", ".join(json.loads(i["themes"] or "[]")) or NA, i["status"],
             i["filter_summary"] or NA, i["instagram_url"] or NA, i["website"] or NA, i["country"] or NA,
             i["audience_age"] or NA, i["audience_gender"] or NA, i["audience_geo"] or NA,
             i["email_source"] or NA, i["data_source"]] for i in infs])

    by_id = {i["id"]: i for i in infs}
    _write(d / "personalized_messages.csv",
           ["Influencer", "Email", "Collab Angle", "Subject", "Email Pitch", "Email Words", "Instagram DM",
            "DM Words", "Generator", "Approved"],
           [[by_id[m["influencer_id"]]["name"], by_id[m["influencer_id"]]["email"] or "Not Found",
             m["collab_angle"], m["email_subject"], m["email_body"], len(m["email_body"].split()),
             m["instagram_dm"], len(m["instagram_dm"].split()), m["generator"],
             "Yes" if m["approved"] else "No"] for m in store.messages() if m["influencer_id"] in by_id])

    rows = store.outreach_rows()
    _write(d / "outreach_tracker.csv",
           ["Influencer", "Channel", "Email", "Message Generated", "Sent", "Date", "Status", "Mode", "Detail"],
           [[r["name"], r["channel"], r["email"] or "Not Found", "Yes" if r["message_generated"] else "No",
             "Yes" if r["sent"] else "No", r["sent_at"] or "", r["status"], r["mode"], r["detail"]] for r in rows])
    return {"influencers": len(infs), "messages": len(store.messages()), "log_rows": len(rows)}
