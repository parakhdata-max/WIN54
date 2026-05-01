def detect_workflow_route(line):

    main_group = line.get("main_group","").lower()
    lens_type = line.get("lens_type","").lower()

    # Contact lens → Vendor
    if "contact" in main_group:
        return "VENDOR"

    # Stock lens
    if line.get("batch_status") == "ALLOCATED":
        return "STOCK"

    # Progressive / bifocal → Surfacing
    if lens_type in ["progressive","bifocal","kryptok"]:
        return "INTERNAL_LAB"

    # Default
    return "VENDOR"
