from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('gg_inventory', 'stock_ops')
class TestStockOperations(TransactionCase):
    """
    Verify core inventory operations: inventory adjustments, receipts, and
    deliveries.  All tests run against the main company to keep setup simple;
    multi-company behaviour is covered in test_multi_company.py.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.location_stock = cls.warehouse.lot_stock_id
        cls.location_input = cls.warehouse.wh_input_stock_loc_id
        cls.location_output = cls.warehouse.wh_output_stock_loc_id
        cls.location_customers = cls.env.ref('stock.stock_location_customers')
        cls.location_suppliers = cls.env.ref('stock.stock_location_suppliers')

        cls.product = cls.env['product.product'].create({
            'name': 'Grove Test Product',
            'type': 'consu',
        })

    # ------------------------------------------------------------------
    # Inventory adjustment
    # ------------------------------------------------------------------

    def test_inventory_adjustment_increases_qty(self):
        """An inventory adjustment quant should reflect the new on-hand qty."""
        self.env['stock.quant']._update_available_quantity(
            self.product,
            self.location_stock,
            10.0,
        )
        qty = self.product.with_context(
            location=self.location_stock.id
        ).qty_available
        self.assertEqual(qty, 10.0, "On-hand qty should be 10 after adjustment")

    def test_inventory_adjustment_decreases_qty(self):
        """Removing stock via adjustment should reduce on-hand qty to zero."""
        self.env['stock.quant']._update_available_quantity(
            self.product, self.location_stock, 5.0
        )
        self.env['stock.quant']._update_available_quantity(
            self.product, self.location_stock, -5.0
        )
        qty = self.product.with_context(
            location=self.location_stock.id
        ).qty_available
        self.assertEqual(qty, 0.0, "On-hand qty should be 0 after removing all stock")

    # ------------------------------------------------------------------
    # Receipt (IN picking)
    # ------------------------------------------------------------------

    def test_receipt_picking_validates_correctly(self):
        """Validating a receipt moves product from supplier → stock location."""
        picking_type_in = self.warehouse.in_type_id
        receipt = self.env['stock.picking'].create({
            'picking_type_id': picking_type_in.id,
            'location_id': self.location_suppliers.id,
            'location_dest_id': self.location_stock.id,
            'move_ids': [(0, 0, {
                'name': self.product.name,
                'product_id': self.product.id,
                'product_uom_qty': 8.0,
                'product_uom': self.product.uom_id.id,
                'location_id': self.location_suppliers.id,
                'location_dest_id': self.location_stock.id,
            })],
        })

        receipt.action_confirm()
        for move in receipt.move_ids:
            move.quantity = 8.0
        receipt.button_validate()

        self.assertEqual(receipt.state, 'done', "Receipt should be in 'done' state after validation")

        qty = self.product.with_context(location=self.location_stock.id).qty_available
        self.assertEqual(qty, 8.0, "Stock should be 8 after validating the receipt")

    # ------------------------------------------------------------------
    # Delivery (OUT picking)
    # ------------------------------------------------------------------

    def test_delivery_reduces_stock(self):
        """Validating a delivery reduces on-hand qty by the delivered amount."""
        # Seed initial stock.
        self.env['stock.quant']._update_available_quantity(
            self.product, self.location_stock, 20.0
        )

        picking_type_out = self.warehouse.out_type_id
        delivery = self.env['stock.picking'].create({
            'picking_type_id': picking_type_out.id,
            'location_id': self.location_stock.id,
            'location_dest_id': self.location_customers.id,
            'move_ids': [(0, 0, {
                'name': self.product.name,
                'product_id': self.product.id,
                'product_uom_qty': 6.0,
                'product_uom': self.product.uom_id.id,
                'location_id': self.location_stock.id,
                'location_dest_id': self.location_customers.id,
            })],
        })

        delivery.action_confirm()
        delivery.action_assign()
        for move in delivery.move_ids:
            move.quantity = 6.0
        delivery.button_validate()

        self.assertEqual(delivery.state, 'done', "Delivery should be in 'done' state after validation")

        qty = self.product.with_context(location=self.location_stock.id).qty_available
        self.assertEqual(qty, 14.0, "Stock should be 14 after delivering 6 from 20")
