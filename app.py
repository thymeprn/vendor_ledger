from flask import Flask, render_template, request, redirect, url_for

from storage import init_db, list_vendors, get_vendor, add_entry, CATEGORIES, CATEGORY_LABELS

app = Flask(__name__)
init_db()


@app.route("/")
def index():
    vendors = list_vendors()
    return render_template("index.html", vendors=vendors)


@app.route("/vendor/<int:vendor_id>")
def vendor_detail(vendor_id):
    vendor = get_vendor(vendor_id)
    if not vendor:
        return "Vendor not found", 404
    return render_template("vendor_detail.html", vendor=vendor, labels=CATEGORY_LABELS)


@app.route("/new", methods=["GET", "POST"])
def new_entry():
    if request.method == "POST":
        vendor_name = request.form.get("vendor_name", "").strip()
        notes = request.form.get("notes", "").strip()
        scores = {c: request.form.get(c) for c in CATEGORIES}

        if vendor_name and all(scores.values()):
            add_entry(vendor_name, scores, notes)
            return redirect(url_for("index"))

    existing_vendors = [v["name"] for v in list_vendors()]
    return render_template("new_entry.html", categories=CATEGORIES, labels=CATEGORY_LABELS,
                            existing_vendors=existing_vendors)


if __name__ == "__main__":
    init_db()
    app.run(port=5001, debug=True)
