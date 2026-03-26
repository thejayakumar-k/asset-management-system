def migrate(cr, version):
    """
    Pre-migration: Clean up old view definitions that reference removed fields.
    
    This runs BEFORE the model definitions are loaded, preventing validation errors.
    Removes cached arch_db for views referencing removed Asset Governance fields.
    """
    if not version:
        return
    
    try:
        # Reset arch_db (compiled view cache) to NULL for all asset.asset views
        # This forces Odoo to recompile from XML, which no longer has the removed fields
        cr.execute("""
            UPDATE ir_ui_view
            SET arch_db = NULL
            WHERE model = 'asset.asset'
        """)
        
        cr.commit()
    except Exception as e:
        # Silently continue if table doesn't exist or other error occurs
        try:
            cr.rollback()
        except:
            pass
