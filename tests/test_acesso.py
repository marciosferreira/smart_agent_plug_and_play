"""Testes offline do controle de acesso (acesso.py), da autenticação (auth.py) e da aplicação
da política em graphql_config — sem rede nem LLM; `graphql_config._post` é substituído."""
import asyncio
import json
import unittest
from unittest import mock

import jwt

import acesso
import auth
import graphql_config

ACL = {
    "enabled": True,
    "default_role": None,
    "users": {
        "1": {"role": "admin", "name": "Ana", "email": "ana@x.com"},
        "2": {"role": "analista", "name": "Bruno", "email": "bruno@x.com"},
        "3": {"role": "sem_nada", "name": "Carla", "email": "carla@x.com"},
    },
    "roles": {
        "admin": {"tables": "*"},
        "analista": {"tables": {
            "dim_site": {},
            "dim_pais": {"rows": [{"field": "ativo", "op": "EQ", "value": True}]},
            "taxa": {
                "columns": {"deny": ["segredo"]},
                "rows": [{"field": "real", "op": "EQ", "value": True}],
            },
        }},
        "sem_nada": {"tables": {}},
    },
    "relations": {"taxa.site_id": "dim_site", "taxa.pais_id": "dim_pais"},
}


def com_acl(acl=ACL):
    return mock.patch.object(acesso, "_carregar", return_value=acl)


class PoliticaTest(unittest.TestCase):
    def test_inativo_nao_restringe(self):
        with com_acl({"enabled": False}):
            self.assertIsNone(acesso.politica_para("2"))
            self.assertEqual(acesso.bloco_prompt("2"), "")

    def test_admin_ve_tudo(self):
        with com_acl():
            pol = acesso.politica_para("1")
            self.assertIsNotNone(pol.regra("qualquer_tabela"))
            self.assertEqual(pol.preparar_tabela("t", [{"field": "x", "op": "EQ", "value": 1}], None, None),
                             [{"field": "x", "op": "EQ", "value": 1}])

    def test_tabela_fora_da_lista_e_negada(self):
        with com_acl():
            with self.assertRaisesRegex(acesso.AcessoNegado, "tabela 'fatura'"):
                acesso.politica_para("2").preparar_tabela("fatura", None, None, None)

    def test_usuario_desconhecido_nega_tudo(self):
        with com_acl():
            for uid in ("999", None, ""):
                with self.assertRaises(acesso.AcessoNegado):
                    acesso.politica_para(uid).preparar_tabela("dim_site", None, None, None)

    def test_default_role_vale_para_desconhecido(self):
        with com_acl({**ACL, "default_role": "analista"}):
            self.assertIsNotNone(acesso.politica_para("999").regra("dim_site"))

    def test_filtro_de_linhas_e_anexado_ao_where(self):
        with com_acl():
            where = acesso.politica_para("2").preparar_tabela(
                "taxa", [{"field": "site_id", "op": "EQ", "value": 5}], None, None)
            self.assertEqual(where, [{"field": "site_id", "op": "EQ", "value": 5},
                                     {"field": "real", "op": "EQ", "value": True}])

    def test_filtro_de_linhas_sem_where_do_usuario(self):
        with com_acl():
            where = acesso.politica_para("2").preparar_tabela("taxa", None, None, None)
            self.assertEqual(where, [{"field": "real", "op": "EQ", "value": True}])

    def test_coluna_negada_em_where_e_order_by(self):
        with com_acl():
            pol = acesso.politica_para("2")
            with self.assertRaisesRegex(acesso.AcessoNegado, "taxa.segredo"):
                pol.preparar_tabela("taxa", [{"field": "segredo", "op": "GT", "value": 1}], None, None)
            with self.assertRaisesRegex(acesso.AcessoNegado, "taxa.segredo"):
                pol.preparar_tabela("taxa", None, {"field": "segredo", "direction": "ASC"}, None)

    def test_coluna_negada_em_agregado(self):
        with com_acl():
            pol = acesso.politica_para("2")
            with self.assertRaises(acesso.AcessoNegado):
                pol.preparar_agregado("taxa", "site_id", "segredo", None, None)
            with self.assertRaises(acesso.AcessoNegado):
                pol.preparar_agregado("taxa", "segredo", "site_id", None, None)

    def test_agregado_aplica_filtro_de_linhas_antes_de_agregar(self):
        with com_acl():
            where = acesso.politica_para("2").preparar_agregado("taxa", "site_id", "valor", None, None)
            self.assertEqual(where, [{"field": "real", "op": "EQ", "value": True}])

    def test_expand_valida_colunas_da_tabela_alvo(self):
        with com_acl({**ACL, "roles": {**ACL["roles"], "analista": {"tables": {
                "taxa": {}, "dim_site": {"columns": {"deny": ["company"]}}}}}}):
            pol = acesso.politica_para("2")
            pol.preparar_tabela("taxa", None, None, [{"viaColumn": "site_id", "columns": ["name"]}])
            with self.assertRaisesRegex(acesso.AcessoNegado, "dim_site.company"):
                pol.preparar_tabela("taxa", None, None, [{"viaColumn": "site_id", "columns": ["company"]}])

    def test_expand_sem_relacao_declarada_e_negado(self):
        with com_acl():
            with self.assertRaisesRegex(acesso.AcessoNegado, "não está declarada"):
                acesso.politica_para("2").preparar_tabela(
                    "taxa", None, None, [{"viaColumn": "outro_id", "columns": ["x"]}])

    def test_expand_em_tabela_alvo_com_filtro_de_linhas_e_negado(self):
        with com_acl():
            with self.assertRaisesRegex(acesso.AcessoNegado, "restrição de linhas"):
                acesso.politica_para("2").preparar_tabela(
                    "taxa", None, None, [{"viaColumn": "pais_id", "columns": ["nome"]}])

    def test_expand_do_agregado_usa_o_group_by_como_fk(self):
        with com_acl():
            pol = acesso.politica_para("2")
            pol.preparar_agregado("taxa", "site_id", "valor", None, ["name"])
            with self.assertRaises(acesso.AcessoNegado):
                pol.preparar_agregado("taxa", "valor", "valor", None, ["name"])

    def test_limpar_linhas_remove_colunas_negadas(self):
        with com_acl():
            linhas = [{"id": 1, "segredo": 9, "site_id__name": "BPL"}]
            self.assertEqual(acesso.politica_para("2").limpar_linhas("taxa", linhas, None),
                             [{"id": 1, "site_id__name": "BPL"}])

    def test_allow_list_de_colunas(self):
        acl = {**ACL, "roles": {**ACL["roles"], "analista": {"tables": {
            "dim_site": {"columns": {"allow": ["id", "name"]}}}}}}
        with com_acl(acl):
            pol = acesso.politica_para("2")
            with self.assertRaises(acesso.AcessoNegado):
                pol.preparar_tabela("dim_site", [{"field": "company", "op": "EQ", "value": "X"}], None, None)
            self.assertEqual(pol.limpar_linhas("dim_site", [{"id": 1, "name": "A", "company": "X"}], None),
                             [{"id": 1, "name": "A"}])

    def test_filtrar_schema(self):
        schema = [
            {"table": "dim_site", "columns": [{"column": "id"}, {"column": "name"}]},
            {"table": "taxa", "columns": [{"column": "id"}, {"column": "segredo"}]},
            {"table": "fatura", "columns": [{"column": "id"}]},
        ]
        with com_acl():
            visto = acesso.politica_para("2").filtrar_schema(schema)
        self.assertEqual([t["table"] for t in visto], ["dim_site", "taxa"])
        self.assertEqual(visto[1]["columns"], [{"column": "id"}])

    def test_descricao_para_o_prompt(self):
        with com_acl():
            texto = acesso.bloco_prompt("2")
        self.assertIn("analista", texto)
        self.assertIn("sem as colunas segredo", texto)
        self.assertIn("real EQ True", texto)


class GraphQLConfigTest(unittest.TestCase):
    """A política é aplicada em run_query_table/run_aggregate/run_schema, antes e depois do POST."""

    def test_query_table_envia_where_efetivo_e_limpa_colunas(self):
        enviado = {}

        def fake_post(query, variables):
            enviado.update(variables)
            return {"reportTable": [{"id": 1, "segredo": 7, "real": True}]}

        with com_acl(), mock.patch.object(graphql_config, "_post", fake_post):
            rows = graphql_config.run_query_table("taxa", user_id="2")
        self.assertEqual(enviado["where"], [{"field": "real", "op": "EQ", "value": True}])
        self.assertEqual(rows, [{"id": 1, "real": True}])  # `segredo` não chega ao chamador

    def test_negado_nao_chega_a_chamar_o_backend(self):
        post = mock.Mock()
        with com_acl(), mock.patch.object(graphql_config, "_post", post):
            with self.assertRaises(acesso.AcessoNegado):
                graphql_config.run_query_table("fatura", user_id="2")
            with self.assertRaises(acesso.AcessoNegado):
                graphql_config.run_aggregate("fatura", "a", "b", "SUM", user_id="2")
        post.assert_not_called()

    def test_usuario_do_contexto_e_usado_quando_nao_ha_user_id_explicito(self):
        post = mock.Mock(return_value={"reportTable": []})
        with com_acl(), mock.patch.object(graphql_config, "_post", post):
            acesso.set_usuario("2")
            try:
                with self.assertRaises(acesso.AcessoNegado):
                    graphql_config.run_query_table("fatura")
            finally:
                acesso.set_usuario(None)
        post.assert_not_called()

    def test_schema_e_filtrado(self):
        dados = {"reportSchema": {"tables": [
            {"table": "dim_site", "columns": [{"column": "id"}]},
            {"table": "fatura", "columns": [{"column": "id"}]},
        ]}}
        with com_acl(), mock.patch.object(graphql_config, "_post", return_value=dados):
            self.assertEqual([t["table"] for t in graphql_config.run_schema(user_id="2")], ["dim_site"])
            self.assertEqual(len(graphql_config.run_schema(user_id="1")), 2)

    def test_sem_acl_nada_muda(self):
        post = mock.Mock(return_value={"reportTable": [{"segredo": 1}]})
        with com_acl({"enabled": False}), mock.patch.object(graphql_config, "_post", post):
            self.assertEqual(graphql_config.run_query_table("qualquer", user_id="2"), [{"segredo": 1}])


class AuthTest(unittest.TestCase):
    def test_token_ida_e_volta_sem_role(self):
        with com_acl():
            token, user = auth.dev_login("2")
        payload = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(set(payload) - {"iat", "exp"}, {"id", "email", "name"})  # papel NÃO vai no token
        self.assertEqual(auth.verify_token(token), user)

    def test_login_de_usuario_desconhecido(self):
        with com_acl():
            self.assertIsNone(auth.dev_login("999"))

    def test_token_expirado_ou_adulterado(self):
        u = auth.CurrentUser("1", "a@x.com", "Ana")
        with self.assertRaises(jwt.ExpiredSignatureError):
            auth.verify_token(auth.create_token(u, ttl=-10))
        with self.assertRaises(jwt.PyJWTError):
            auth.verify_token(auth.create_token(u) + "x")
        with self.assertRaises(jwt.PyJWTError):
            auth.verify_token(jwt.encode({"id": "1", "email": "a", "name": "A"}, "outro-segredo", algorithm="HS256"))


class MiddlewareTest(unittest.TestCase):
    """O middleware exige o token nas rotas protegidas e impõe o user_id do token."""

    def _chamar(self, path, *, headers=None, query=b"", body=None, method="POST"):
        visto = {}

        async def app(scope, receive, send):
            visto["query"] = scope["query_string"].decode()
            visto["state"] = dict(scope.get("state", {}))
            msg = await receive()
            visto["body"] = msg.get("body", b"")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        enviados = []

        async def send(m):
            enviados.append(m)

        async def receive():
            return {"type": "http.request", "body": body or b"", "more_body": False}

        scope = {"type": "http", "method": method, "path": path, "query_string": query,
                 "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
        asyncio.run(auth.AuthMiddleware(app)(scope, receive, send))
        return enviados[0]["status"], visto

    def _token(self, uid="2"):
        with com_acl():
            return auth.dev_login(uid)[0]

    def test_sem_token_401(self):
        with com_acl():
            status, _ = self._chamar("/chat", body=b"{}")
        self.assertEqual(status, 401)

    def test_token_invalido_401(self):
        with com_acl():
            status, _ = self._chamar("/tasks", headers={"authorization": "Bearer lixo"}, method="GET")
        self.assertEqual(status, 401)

    def test_user_id_da_query_e_forcado_para_o_do_token(self):
        with com_acl():
            status, visto = self._chamar(
                "/tasks", headers={"authorization": f"Bearer {self._token('2')}"},
                query=b"user_id=1&x=9", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("user_id=2", visto["query"])
        self.assertNotIn("user_id=1", visto["query"])
        self.assertIn("x=9", visto["query"])
        self.assertEqual(visto["state"]["user_id"], "2")

    def test_user_id_do_corpo_json_e_forcado(self):
        with com_acl():
            status, visto = self._chamar(
                "/chat", headers={"authorization": f"Bearer {self._token('2')}",
                                  "content-type": "application/json"},
                body=json.dumps({"message": "oi", "user_id": "1"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(visto["body"]), {"message": "oi", "user_id": "2"})

    def test_token_na_query_para_eventsource(self):
        with com_acl():
            status, visto = self._chamar(
                "/chat/stream", query=f"message=oi&user_id=1&access_token={self._token('2')}".encode(),
                method="GET")
        self.assertEqual(status, 200)
        self.assertIn("user_id=2", visto["query"])
        self.assertNotIn("access_token", visto["query"])

    def test_rotas_abertas_e_acl_inativa_passam_sem_token(self):
        with com_acl():
            self.assertEqual(self._chamar("/app-info", method="GET")[0], 200)
            self.assertEqual(self._chamar("/auth/dev-login", body=b"{}")[0], 200)
        with com_acl({"enabled": False}):
            status, visto = self._chamar("/chat", query=b"user_id=1", body=b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(visto["query"], "user_id=1")  # sem ACL o app é o de sempre


if __name__ == "__main__":
    unittest.main()
