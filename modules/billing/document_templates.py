"""
Document Templates for Challans and Invoices
==============================================

Provides HTML templates for printing challans and invoices.
"""

import streamlit as st
from typing import Dict, List
from datetime import datetime, date
import pandas as pd


def _q(sql: str, params: dict = None) -> List[Dict]:
    """Database query helper"""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"Database error: {e}")
        return []


def format_currency(amount: float) -> str:
    """Format currency with Indian rupee symbol"""
    try:
        return f"₹{float(amount or 0):,.2f}"
    except Exception:
        return "₹0.00"


def format_date(date_val) -> str:
    """Format date for display"""
    if isinstance(date_val, str):
        try:
            dt = datetime.strptime(date_val[:10], "%Y-%m-%d")
            return dt.strftime("%d %b %Y")
        except:
            return date_val[:10]
    elif isinstance(date_val, date):
        return date_val.strftime("%d %b %Y")
    return str(date_val)


def render_challan_template(challan_no: str):
    """Render HTML template for challan printing"""
    
    # Get challan details
    challan_data = _q("""
        SELECT c.*,
               COALESCE(p.party_name, 'Walk-in') AS party_name,
               COALESCE(p.mobile, '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.gstin, '') AS gst_no,
               p.email, p.contact_person
        FROM challans c
        LEFT JOIN parties p ON p.id = c.party_id
        WHERE c.challan_no = %(challan_no)s
    """, {"challan_no": challan_no})
    
    if not challan_data:
        st.error(f"❌ Challan {challan_no} not found")
        return
    
    challan = challan_data[0]
    
    # Get challan line items
    lines = _q("""
        SELECT cl.*,
               COALESCE(o.order_no, '')    AS order_no,
               o.created_at AS order_date,
               COALESCE(cl.product_name, pr.product_name, '') AS product_name,
               cl.quantity,
               cl.unit_price,
               cl.line_total              AS total_price,
               COALESCE(cl.brand, pr.brand, '') AS brand,
               COALESCE(pr.box_size, 1)   AS box_size,
               COALESCE(pr.unit, 'PCS')   AS unit,
               COALESCE(cl.eye_side, ol.eye_side, '') AS eye_side,
               ol.sph       AS sph,
               ol.cyl       AS cyl,
               ol.axis      AS axis,
               ol.add_power AS add_power
        FROM challan_lines cl
        LEFT JOIN orders o       ON o.id  = cl.order_id
        LEFT JOIN order_lines ol ON ol.id = cl.order_line_id
        LEFT JOIN products pr    ON pr.id = ol.product_id
        WHERE cl.challan_id = %(challan_id)s
          AND COALESCE(cl.is_deleted, FALSE) = FALSE
        ORDER BY cl.id
    """, {"challan_id": challan["id"]})

    # Get service charge lines from snapshot
    svc_lines = _q("""
        SELECT charge_type, description, base_amount,
               gst_percent, gst_amount, total_amount
        FROM challan_service_charges
        WHERE challan_id = %(challan_id)s
        ORDER BY created_at
    """, {"challan_id": challan["id"]})
    
    # Generate HTML template
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Challan {challan['challan_no']}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 20px;
                background: white;
            }}
            .header {{
                text-align: center;
                border-bottom: 2px solid #333;
                padding-bottom: 20px;
                margin-bottom: 30px;
            }}
            .company-name {{
                font-size: 24px;
                font-weight: bold;
                margin-bottom: 5px;
            }}
            .document-title {{
                font-size: 20px;
                font-weight: bold;
                color: #333;
                margin: 20px 0;
            }}
            .two-column {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 30px;
            }}
            .column {{
                width: 48%;
            }}
            .info-label {{
                font-weight: bold;
                margin-bottom: 5px;
            }}
            .info-value {{
                margin-bottom: 15px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 20px;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
                font-weight: bold;
            }}
            .total-row {{
                font-weight: bold;
                background-color: #f9f9f9;
            }}
            .summary {{
                margin-top: 30px;
                text-align: right;
            }}
            .summary-item {{
                margin-bottom: 10px;
                font-size: 16px;
            }}
            .grand-total {{
                font-size: 18px;
                font-weight: bold;
                border-top: 2px solid #333;
                padding-top: 10px;
            }}
            .footer {{
                margin-top: 50px;
                text-align: center;
                font-size: 12px;
                color: #666;
            }}
            .signature {{
                margin-top: 50px;
                display: flex;
                justify-content: space-between;
            }}
            .signature-box {{
                width: 200px;
                text-align: center;
            }}
            .signature-line {{
                border-bottom: 1px solid #333;
                margin-bottom: 5px;
                height: 40px;
            }}
            @media print {{
                body {{ margin: 0; }}
                .no-print {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <div class="company-name">AHUJA OPTICAL CO</div>
            <div>Optical Solutions Provider</div>
            <div style="margin-top: 10px; font-size: 12px;">
                GST: 07AAAPK1234C1ZV | Phone: +91-98100-12345 | Email: info@ahujaoptical.com
            </div>
        </div>
        
        <div class="document-title">DELIVERY CHALLAN</div>
        
        <div class="two-column">
            <div class="column">
                <div class="info-label">Challan No:</div>
                <div class="info-value">{challan['challan_no']}</div>
                
                <div class="info-label">Date:</div>
                <div class="info-value">{format_date(challan['challan_date'])}</div>
                
                <div class="info-label">Status:</div>
                <div class="info-value">{challan['status']}</div>
            </div>
            
            <div class="column">
                <div class="info-label">Party Name:</div>
                <div class="info-value">{challan['party_name']}</div>
                
                <div class="info-label">Mobile:</div>
                <div class="info-value">{challan['mobile']}</div>
                
                {f'''<div class="info-label">GST No:</div>
                <div class="info-value">{challan['gst_no']}</div>''' if challan.get('gst_no') else ''}
                
                {f'''<div class="info-label">Address:</div>
                <div class="info-value">{challan['address']}</div>''' if challan.get('address') else ''}
            </div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>Order No</th>
                    <th>Product</th>
                    <th>Brand</th>
                    <th>Quantity</th>
                    <th>Unit Price</th>
                    <th>Total</th>
                </tr>
            </thead>
            <tbody>
    """
    
    # Add line items
    for line in lines:
        # Format quantity with box logic
        qty = line['quantity']
        box_size = line.get('box_size', 1)
        unit = line.get('unit', 'PCS').upper()
        
        if unit == 'BOX' and box_size > 1:
            if qty == box_size:
                qty_display = "1 BOX"
            elif qty > box_size:
                boxes = qty // box_size
                pcs_rem = qty % box_size
                if pcs_rem == 0:
                    qty_display = f"{boxes} BOX"
                else:
                    qty_display = f"{boxes} BOX + {pcs_rem} PCS"
            else:
                qty_display = f"{qty} PCS"
        else:
            qty_display = f"{qty} PCS"
        
        html_template += f"""
                <tr>
                    <td>{line['order_no']}</td>
                    <td>{line['product_name']}</td>
                    <td>{line.get('brand', '')}</td>
                    <td>{qty_display}</td>
                    <td>{format_currency(line['unit_price'])}</td>
                    <td>{format_currency(line['total_price'])}</td>
                </tr>
        """
    
    html_template += f"""
            </tbody>
        </table>
        
        <div class="summary">
            <div class="summary-item">Subtotal: {format_currency(challan['total_amount'])}</div>
            <div class="summary-item">Tax (18%): {format_currency(challan['total_tax'])}</div>
            <div class="summary-item grand-total">Grand Total: {format_currency(challan['grand_total'])}</div>
        </div>
        
        {f'''<div style="margin-top: 20px;">
            <strong>Remarks:</strong> {challan['remarks']}
        </div>''' if challan.get('remarks') else ''}
        
        <div class="signature">
            <div class="signature-box">
                <div class="signature-line"></div>
                <div>Receiver Signature</div>
            </div>
            <div class="signature-box">
                <div class="signature-line"></div>
                <div>Authorized Signature</div>
            </div>
        </div>
        
        <div class="footer">
            <div>This is a computer generated document</div>
            <div>Generated on: {datetime.now().strftime("%d %b %Y at %I:%M %p")}</div>
        </div>
    </body>
    </html>
    """
    
    return html_template


def render_invoice_template(invoice_no: str):
    """Render HTML template for invoice printing"""
    
    # Get invoice details
    invoice_data = _q("""
        SELECT i.*,
               COALESCE(p.party_name, 'Walk-in') AS party_name,
               COALESCE(p.mobile, '') AS mobile,
               COALESCE(p.address, '') AS address,
               COALESCE(p.gstin, '') AS gst_no,
               p.email, p.contact_person,
               c.challan_no
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        LEFT JOIN challans c ON c.id = i.challan_id
        WHERE i.invoice_no = %(invoice_no)s
    """, {"invoice_no": invoice_no})
    
    if not invoice_data:
        st.error(f"❌ Invoice {invoice_no} not found")
        return
    
    invoice = invoice_data[0]
    
    # Get invoice line items
    lines = _q("""
        SELECT il.*,
               COALESCE(o.order_no, '')   AS order_no,
               o.created_at AS order_date,
               COALESCE(il.product_name, pr.product_name, '') AS product_name,
               il.quantity,
               il.unit_price,
               COALESCE(il.total_price, il.line_total, 0) AS total_price,
               COALESCE(il.brand, pr.brand, '') AS brand,
               COALESCE(pr.box_size, 1)   AS box_size,
               COALESCE(pr.unit, 'PCS')   AS unit,
               COALESCE(il.eye_side, ol.eye_side, '') AS eye_side,
               ol.sph       AS sph,
               ol.cyl       AS cyl,
               ol.axis      AS axis,
               ol.add_power AS add_power
        FROM invoice_lines il
        LEFT JOIN orders o       ON o.id  = il.order_id
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN products pr    ON pr.id = ol.product_id
        WHERE il.invoice_id = %(invoice_id)s
          AND COALESCE(il.is_deleted, FALSE) = FALSE
        ORDER BY il.id
    """, {"invoice_id": invoice["id"]})
    
    # Generate HTML template
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Invoice {invoice['invoice_no']}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 20px;
                background: white;
            }}
            .header {{
                text-align: center;
                border-bottom: 2px solid #333;
                padding-bottom: 20px;
                margin-bottom: 30px;
            }}
            .company-name {{
                font-size: 24px;
                font-weight: bold;
                margin-bottom: 5px;
            }}
            .document-title {{
                font-size: 20px;
                font-weight: bold;
                color: #333;
                margin: 20px 0;
            }}
            .two-column {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 30px;
            }}
            .column {{
                width: 48%;
            }}
            .info-label {{
                font-weight: bold;
                margin-bottom: 5px;
            }}
            .info-value {{
                margin-bottom: 15px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 20px;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
                font-weight: bold;
            }}
            .total-row {{
                font-weight: bold;
                background-color: #f9f9f9;
            }}
            .summary {{
                margin-top: 30px;
                text-align: right;
            }}
            .summary-item {{
                margin-bottom: 10px;
                font-size: 16px;
            }}
            .grand-total {{
                font-size: 18px;
                font-weight: bold;
                border-top: 2px solid #333;
                padding-top: 10px;
            }}
            .payment-status {{
                margin-top: 20px;
                padding: 10px;
                text-align: center;
                font-weight: bold;
                border-radius: 5px;
            }}
            .paid {{
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }}
            .unpaid {{
                background-color: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }}
            .footer {{
                margin-top: 50px;
                text-align: center;
                font-size: 12px;
                color: #666;
            }}
            .signature {{
                margin-top: 50px;
                display: flex;
                justify-content: space-between;
            }}
            .signature-box {{
                width: 200px;
                text-align: center;
            }}
            .signature-line {{
                border-bottom: 1px solid #333;
                margin-bottom: 5px;
                height: 40px;
            }}
            @media print {{
                body {{ margin: 0; }}
                .no-print {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <div class="company-name">AHUJA OPTICAL CO</div>
            <div>Tax Invoice</div>
            <div style="margin-top: 10px; font-size: 12px;">
                GST: 07AAAPK1234C1ZV | Phone: +91-98100-12345 | Email: info@ahujaoptical.com
            </div>
        </div>
        
        <div class="document-title">TAX INVOICE</div>
        
        <div class="two-column">
            <div class="column">
                <div class="info-label">Invoice No:</div>
                <div class="info-value">{invoice['invoice_no']}</div>
                
                <div class="info-label">Invoice Date:</div>
                <div class="info-value">{format_date(invoice['invoice_date'])}</div>
                
                <div class="info-label">Due Date:</div>
                <div class="info-value">{format_date(invoice['due_date'])}</div>
                
                <div class="info-label">Status:</div>
                <div class="info-value">{invoice['status']}</div>
                
                {f'''<div class="info-label">Challan No:</div>
                <div class="info-value">{invoice['challan_no']}</div>''' if invoice.get('challan_no') else ''}
            </div>
            
            <div class="column">
                <div class="info-label">Party Name:</div>
                <div class="info-value">{invoice['party_name']}</div>
                
                <div class="info-label">Mobile:</div>
                <div class="info-value">{invoice['mobile']}</div>
                
                {f'''<div class="info-label">GST No:</div>
                <div class="info-value">{invoice['gst_no']}</div>''' if invoice.get('gst_no') else ''}
                
                {f'''<div class="info-label">Address:</div>
                <div class="info-value">{invoice['address']}</div>''' if invoice.get('address') else ''}
            </div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>Order No</th>
                    <th>Product</th>
                    <th>Brand</th>
                    <th>Quantity</th>
                    <th>Unit Price</th>
                    <th>Tax Amount</th>
                    <th>Total</th>
                </tr>
            </thead>
            <tbody>
    """
    
    # Add line items
    for line in lines:
        # Format quantity with box logic
        qty = line['quantity']
        box_size = line.get('box_size', 1)
        unit = line.get('unit', 'PCS').upper()
        
        if unit == 'BOX' and box_size > 1:
            if qty == box_size:
                qty_display = "1 BOX"
            elif qty > box_size:
                boxes = qty // box_size
                pcs_rem = qty % box_size
                if pcs_rem == 0:
                    qty_display = f"{boxes} BOX"
                else:
                    qty_display = f"{boxes} BOX + {pcs_rem} PCS"
            else:
                qty_display = f"{qty} PCS"
        else:
            qty_display = f"{qty} PCS"
        
        tax_amount = line.get('tax_amount', 0)
        line_total = line.get('line_total', line['total_price'])
        
        html_template += f"""
                <tr>
                    <td>{line['order_no']}</td>
                    <td>{line['product_name']}</td>
                    <td>{line.get('brand', '')}</td>
                    <td>{qty_display}</td>
                    <td>{format_currency(line['unit_price'])}</td>
                    <td>{format_currency(tax_amount)}</td>
                    <td>{format_currency(line_total)}</td>
                </tr>
        """
    
    payment_status_class = "paid" if invoice['payment_status'] == 'PAID' else "unpaid"
    payment_status_text = "PAID" if invoice['payment_status'] == 'PAID' else "UNPAID"
    
    html_template += f"""
            </tbody>
        </table>
        
        <div class="summary">
            <div class="summary-item">Subtotal: {format_currency(invoice['total_amount'])}</div>
            <div class="summary-item">Tax (18%): {format_currency(invoice['total_tax'])}</div>
            <div class="summary-item grand-total">Grand Total: {format_currency(invoice['grand_total'])}</div>
        </div>
        
        <div class="payment-status {payment_status_class}">
            Payment Status: {payment_status_text}
        </div>
        
        {f'''<div style="margin-top: 20px;">
            <strong>Remarks:</strong> {invoice['remarks']}
        </div>''' if invoice.get('remarks') else ''}
        
        <div class="signature">
            <div class="signature-box">
                <div class="signature-line"></div>
                <div>Receiver Signature</div>
            </div>
            <div class="signature-box">
                <div class="signature-line"></div>
                <div>Authorized Signature</div>
            </div>
        </div>
        
        <div class="footer">
            <div>This is a computer generated document</div>
            <div>Generated on: {datetime.now().strftime("%d %b %Y at %I:%M %p")}</div>
        </div>
    </body>
    </html>
    """
    
    return html_template


def render_print_preview(document_type: str, document_no: str):
    """Render print preview for document"""
    
    if document_type == "challan":
        html_content = render_challan_template(document_no)
        title = f"Challan {document_no}"
    elif document_type == "invoice":
        html_content = render_invoice_template(document_no)
        title = f"Invoice {document_no}"
    else:
        st.error("Invalid document type")
        return
    
    st.markdown(f"### 🖨️ Print Preview: {title}")
    
    # Display HTML content
    st.components.v1.html(html_content, height=800, scrolling=True)
    
    # Print options
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🖨️ Print", type="primary", use_container_width=True):
            try:
                from modules.printing.print_opener import open_html_print

                path = open_html_print(html_content, f"{document_type}_{document_no}.html")
                st.success(f"Print document opened: {path}")
            except Exception as exc:
                st.error(f"Print open failed: {exc}")
    
    with col2:
        if st.button("💾 Download as HTML", use_container_width=True):
            st.download_button(
                label="📄 Download HTML",
                data=html_content,
                file_name=f"{document_type}_{document_no}.html",
                mime="text/html"
            )
    
    with col3:
        if st.button("📧 Email Document", use_container_width=True):
            st.info("📧 Email functionality coming soon...")


def add_print_buttons_to_preview():
    """Add print buttons to document preview sections"""
    
    # This function can be called from the preview functions
    # to add consistent print buttons
    
    st.markdown("---")
    st.markdown("### 🖨️ Print Options")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🖨️ Print Document", type="primary", use_container_width=True):
            st.info("🖨️ Use browser's print function (Ctrl+P) to print this document")
    
    with col2:
        if st.button("📄 Download PDF", use_container_width=True):
            st.info("📄 PDF download coming soon...")
    
    with col3:
        if st.button("📧 Email Document", use_container_width=True):
            st.info("📧 Email functionality coming soon...")
