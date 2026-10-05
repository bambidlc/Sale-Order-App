import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import run_orders_automation
from src import network_resilience
from src.odoo_sync import OdooApiError, OdooAmbiguousWriteError, OdooJson2Client, OdooSalesOrderSyncService
from src.outlook_fetcher import MicrosoftGraphClient, OutlookNoMessagesError


class ConnectionResilienceTests(unittest.TestCase):
    def setUp(self):
        network_resilience._cooldowns.clear()

    def graph_client(self):
        return MicrosoftGraphClient(SimpleNamespace(tenant_id='t', client_id='c', client_secret='s'))

    @patch('src.network_resilience.time.sleep')
    def test_auth_recovers_after_dns_failure(self, sleep):
        client = self.graph_client()
        response = Mock(status_code=200, headers={}, content=b'json')
        response.json.return_value = {'access_token': 'test', 'expires_in': 3600}
        client.session.post = Mock(side_effect=[requests.ConnectionError(), response])
        self.assertEqual(client._access_token(), 'test')
        self.assertEqual(client.session.post.call_count, 2)

    def test_long_run_refreshes_expired_token(self):
        client = self.graph_client()
        client._token = 'expired'
        client._token_expires_at = 1
        response = Mock(status_code=200, headers={}, content=b'json')
        response.json.return_value = {'access_token': 'fresh', 'expires_in': 3600}
        client.session.post = Mock(return_value=response)
        self.assertEqual(client._access_token(), 'fresh')
        client.session.post.assert_called_once()

    def test_order_create_is_not_retried_after_timeout(self):
        client = OdooJson2Client(SimpleNamespace(odoo_base_url='https://example.test',
                                odoo_api_token='test', odoo_database='test'), logging.getLogger('test'))
        client.session.post = Mock(side_effect=requests.Timeout())
        with self.assertRaises(OdooApiError):
            client.create('sale.order', {})
        client.session.post.assert_called_once()

    def test_header_and_lines_are_created_in_one_transaction(self):
        service = OdooSalesOrderSyncService.__new__(OdooSalesOrderSyncService)
        service.client = Mock()
        service.client.create.return_value = 42
        service._find_existing_order = Mock(return_value=None)
        service._get_sale_order_line_fields = Mock(return_value={'product_uom_id': {}})
        service._ensure_partner = Mock(return_value=({'id': 1}, None))
        service._ensure_product = Mock(return_value=(SimpleNamespace(product_id=2, uom_id=3), None))
        service._build_order_values = Mock(return_value={'partner_id': 1})
        draft = SimpleNamespace(order_name='test', source_document='test', customer_number='1',
                                customer_name='Test', salesperson_name='Test',
                                lines=[SimpleNamespace(sku='sku', quantity=2, unit_price=3, product_display='Demo'),
                                       SimpleNamespace(sku='sku2', quantity=4, unit_price=5, product_display='Demo 2')])
        result = service._sync_order(draft, dry_run=False)
        self.assertEqual(result.status, 'created')
        service.client.create.assert_called_once()
        model, values = service.client.create.call_args.args
        self.assertEqual(model, 'sale.order')
        self.assertEqual(len(values['order_line']), 2)
        self.assertEqual(values['order_line'][0][:2], [0, 0])
        self.assertNotIn('order_id', values['order_line'][0][2])
        self.assertEqual(values['order_line'][1][2]['product_uom_qty'], 4)
        service.client.unlink.assert_not_called()

    @patch('src.odoo_sync.parse_rows', return_value=[])
    @patch('src.odoo_sync.build_sales_order_drafts')
    def test_uncertain_write_stops_before_cached_misses_can_create_duplicates(self, build, parse):
        service = OdooSalesOrderSyncService.__new__(OdooSalesOrderSyncService)
        service.logger = Mock()
        service.state_store = Mock()
        service.state_store.is_successfully_synced_report.return_value = False
        service._sync_order = Mock(side_effect=OdooAmbiguousWriteError('lost response'))
        draft = SimpleNamespace(order_name='test', source_document='test', customer_number='1',
                                customer_name='Test', salesperson_name='Test', lines=[1])
        build.return_value = [draft, draft, draft]
        from pathlib import Path
        report = SimpleNamespace(path=Path('test.xlsx'), modified_at='2026-10-05', size_bytes=1, fingerprint='abc')
        summary = service.sync_file('test.xlsx', report=report)
        self.assertEqual(summary['status'], 'partial_success')
        self.assertEqual(summary['failed_orders'], 1)
        service._sync_order.assert_called_once()

    @patch('run_orders_automation.OdooSalesOrderSyncService')
    @patch('run_orders_automation.OutlookFetcherService')
    @patch('sys.argv', ['run_orders_automation.py'])
    def test_failed_report_preserves_source_and_stops_batch(self, fetch, sync):
        fetch.return_value.fetch_latest_report.return_value = {'download_path': 'test.xlsx'}
        sync.return_value.sync_file.return_value = {'failed_orders': 1, 'status': 'partial_success'}
        self.assertEqual(run_orders_automation.main(), 1)
        fetch.return_value.complete_processing.assert_not_called()
        fetch.return_value.fetch_latest_report.assert_called_once()

    @patch('run_orders_automation.OdooSalesOrderSyncService')
    @patch('run_orders_automation.OutlookFetcherService')
    @patch('sys.argv', ['run_orders_automation.py'])
    def test_each_report_processed_sequentially(self, fetch, sync):
        reports = [{'download_path': 'first.xlsx'}, {'download_path': 'second.xlsx'}]
        fetch.return_value.fetch_latest_report.side_effect = reports + [OutlookNoMessagesError()]
        sync.return_value.sync_file.return_value = {'status': 'success', 'failed_orders': 0}
        self.assertEqual(run_orders_automation.main(), 0)
        self.assertEqual([call.args[0] for call in sync.return_value.sync_file.call_args_list],
                         ['first.xlsx', 'second.xlsx'])
        self.assertEqual([call.args[0] for call in fetch.return_value.complete_processing.call_args_list], reports)


if __name__ == '__main__':
    unittest.main()
