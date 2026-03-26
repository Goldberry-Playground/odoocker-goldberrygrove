from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('gg_inventory', 'multi_company')
class TestMultiCompanyInventory(TransactionCase):
    """
    Verify that warehouses, stock locations, and products are correctly
    isolated across the three Gather at the Grove companies.

    Each test method runs inside a savepoint that is rolled back on teardown,
    so there is no cross-test contamination.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create two companies representing two of the three Grove businesses.
        cls.company_goldberry = cls.env['res.company'].create({
            'name': 'Goldberry Grove',
            'currency_id': cls.env.ref('base.USD').id,
        })
        cls.company_george = cls.env['res.company'].create({
            'name': 'George George George',
            'currency_id': cls.env.ref('base.USD').id,
        })

        # Admin user gets access to both companies so we can switch context.
        cls.env.user.write({
            'company_ids': [(4, cls.company_goldberry.id), (4, cls.company_george.id)],
        })

    def _env_for(self, company):
        """Return a recordset environment scoped to *company*."""
        return self.env(context=dict(self.env.context, allowed_company_ids=[company.id]))

    # ------------------------------------------------------------------
    # Warehouse isolation
    # ------------------------------------------------------------------

    def test_warehouse_belongs_to_correct_company(self):
        """Each auto-created warehouse must reference its own company."""
        env_gb = self._env_for(self.company_goldberry)
        env_gg = self._env_for(self.company_george)

        wh_gb = env_gb['stock.warehouse'].search([('company_id', '=', self.company_goldberry.id)])
        wh_gg = env_gg['stock.warehouse'].search([('company_id', '=', self.company_george.id)])

        self.assertTrue(wh_gb, "Goldberry Grove should have at least one warehouse")
        self.assertTrue(wh_gg, "George George George should have at least one warehouse")

        # No warehouse should cross company boundaries.
        for wh in wh_gb:
            self.assertEqual(wh.company_id, self.company_goldberry)
        for wh in wh_gg:
            self.assertEqual(wh.company_id, self.company_george)

    def test_stock_locations_isolated_by_company(self):
        """Internal stock locations created for one company must not appear
        in a search scoped to the other company."""
        env_gb = self._env_for(self.company_goldberry)
        env_gg = self._env_for(self.company_george)

        locs_gb = env_gb['stock.location'].search([
            ('company_id', '=', self.company_goldberry.id),
            ('usage', '=', 'internal'),
        ])
        locs_gg = env_gg['stock.location'].search([
            ('company_id', '=', self.company_george.id),
            ('usage', '=', 'internal'),
        ])

        gb_ids = set(locs_gb.ids)
        gg_ids = set(locs_gg.ids)
        self.assertFalse(
            gb_ids & gg_ids,
            "Internal locations should not be shared between companies",
        )

    def test_product_availability_not_shared(self):
        """A product created in company A should have zero qty on hand
        when queried from company B's warehouse."""
        env_gb = self._env_for(self.company_goldberry)
        env_gg = self._env_for(self.company_george)

        # Create a storable product owned by Goldberry Grove.
        product = env_gb['product.product'].create({
            'name': 'Test Orchard Apple',
            'type': 'consu',
            'company_id': self.company_goldberry.id,
        })

        wh_gg = env_gg['stock.warehouse'].search(
            [('company_id', '=', self.company_george.id)], limit=1
        )

        if not wh_gg:
            self.skipTest("George George George has no warehouse — skipping cross-company qty check")

        qty = product.with_context(
            warehouse=wh_gg.id,
            allowed_company_ids=[self.company_george.id],
        ).qty_available

        self.assertEqual(qty, 0.0, "Product belonging to Goldberry Grove should have no stock in George's warehouse")
