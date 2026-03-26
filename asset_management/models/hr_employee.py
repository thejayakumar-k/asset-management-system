from odoo import models, fields, api

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    assigned_asset_ids = fields.One2many(
        'asset.asset',
        'assigned_employee_id',
        string='Assigned Assets'
    )

    asset_assignment_history_ids = fields.One2many(
        'asset.assignment.history',
        'employee_id',
        string='Asset Assignment History'
    )
