import { BadRequestException, UseGuards } from '@nestjs/common';
import { Args, Int, Query, Resolver } from '@nestjs/graphql';
import { GraphQLJSONObject } from 'graphql-type-json';
import { PrismaService } from 'src/infra/database/prisma/prisma.service';
import { AnalyticsDecryptionService } from '../analytics-decryption.service';
import {
  ANALYTICS_RELATIONS,
  ANALYTICS_SCHEMA,
  ENCRYPTED_COLUMNS,
  MAX_AGGREGATE_SCAN_ROWS,
} from '../analytics.constants';
import { AnalyticsApiKeyGuard } from '../guards/analytics-api-key.guard';
import { ColumnInfo } from './column-info.type';
import { AggregateFunction, FilterOperator, SortDirection, SQL_FILTER_OPERATOR } from './report-aggregate.enums';
import { ExpandInput, FilterInput, ReportAggregateInput, SortInput } from './report-aggregate.inputs';
import { SchemaInfo } from './schema-info.type';
import { TableRelation } from './table-relation.type';

const MAX_AGGREGATE_LIMIT = 500;
const DEFAULT_TABLE_LIMIT = 100;
const MAX_TABLE_LIMIT = 1000;
const CATALOG_TTL_MS = 5 * 60 * 1000;

interface ColumnMeta {
  column: string;
  dataType: string;
  nullable: boolean;
  description: string | null;
}

interface Catalog {
  tables: string[];
  tableDescriptions: Map<string, string | null>;
  columnsByTable: Map<string, ColumnMeta[]>;
  loadedAt: number;
}

/**
 * Motor de consulta generico sobre a fachada de analytics.
 *
 * Nao aceita SQL livre: tabela, coluna, operador e funcao de agregacao sao sempre validados
 * contra o catalogo do banco antes de qualquer interpolacao, e os VALORES vao por placeholder.
 * Identificadores nao podem ser parametrizados em SQL -- validar contra o catalogo e entao
 * aspear e a forma correta.
 *
 * Duas restricoes decorrem da criptografia em repouso:
 *  - colunas sensiveis chegam do banco como JSONB ciphertext e sao decifradas AQUI, depois da
 *    consulta, apenas para o recorte de linhas retornado;
 *  - por isso o banco nao consegue filtrar, ordenar nem agregar por essas colunas. Filtro e
 *    ordenacao sobre elas sao recusados; agregacao acontece em memoria.
 */
@UseGuards(AnalyticsApiKeyGuard)
@Resolver()
export class ReportResolver {
  private catalogCache: Catalog | null = null;
  private catalogInFlight: Promise<Catalog> | null = null;

  constructor(
    private readonly prisma: PrismaService,
    private readonly decryption: AnalyticsDecryptionService,
  ) {}

  @Query(() => [String], {
    description: 'Lista as views do modelo de analytics disponíveis para consulta.',
  })
  async reportTables(): Promise<string[]> {
    const catalog = await this.getCatalog();
    return catalog.tables;
  }

  @Query(() => SchemaInfo, {
    description:
      'Retorna de uma vez a estrutura completa do modelo de analytics, agrupada por tabela: cada tabela já vem com sua descrição, suas colunas (nome, tipo, se aceita nulo, descrição) e seus relacionamentos (foreign keys de saída e de entrada). Use isso primeiro para entender o modelo de dados antes de montar consultas.',
  })
  async reportSchema(): Promise<SchemaInfo> {
    const catalog = await this.getCatalog();

    const tables = catalog.tables.map(table => ({
      table,
      description: catalog.tableDescriptions.get(table) ?? null,
      columns: (catalog.columnsByTable.get(table) ?? []).map(
        (meta): ColumnInfo => ({
          column: meta.column,
          dataType: this.isEncrypted(table, meta.column) ? 'numeric' : meta.dataType,
          nullable: meta.nullable,
          description: this.describeColumn(table, meta),
        }),
      ),
      relations: ANALYTICS_RELATIONS.filter(relation => relation.fromTable === table || relation.toTable === table).map(
        relation => ({ ...relation }),
      ),
    }));

    return { tables };
  }

  @Query(() => [TableRelation], {
    description:
      'Lista os relacionamentos de uma tabela, nos dois sentidos: colunas dessa tabela que referenciam outras, e colunas de outras tabelas que referenciam essa.',
  })
  async reportTableRelations(@Args('table', { type: () => String }) table: string): Promise<TableRelation[]> {
    const catalog = await this.getCatalog();
    this.assertTableExists(table, catalog);

    return ANALYTICS_RELATIONS.filter(relation => relation.fromTable === table || relation.toTable === table).map(
      relation => ({ ...relation }),
    );
  }

  @Query(() => [GraphQLJSONObject], {
    description:
      'Retorna os registros de uma tabela, com filtros/ordenação/paginação opcionais e, opcionalmente, colunas de tabelas relacionadas (via foreign key) já juntadas em cada linha. O "limit" tem padrão 100 e teto 1000; use "offset" para paginar. Colunas sensíveis não aceitam filtro nem ordenação (ver reportSchema).',
  })
  async reportTable(
    @Args('table', { type: () => String }) table: string,
    @Args('where', { type: () => [FilterInput], nullable: true })
    where?: FilterInput[],
    @Args('orderBy', { type: () => SortInput, nullable: true })
    orderBy?: SortInput,
    @Args('limit', { type: () => Int, nullable: true }) limit?: number,
    @Args('offset', { type: () => Int, nullable: true }) offset?: number,
    @Args('expand', { type: () => [ExpandInput], nullable: true })
    expand?: ExpandInput[],
  ): Promise<Record<string, unknown>[]> {
    const catalog = await this.getCatalog();
    this.assertTableExists(table, catalog);

    const columns = this.getColumnNames(table, catalog);
    for (const filter of where ?? []) {
      this.assertColumnExists(table, filter.field, columns);
      this.assertNotEncrypted(table, filter.field, 'filtrada');
    }

    // Checar `orderBy?.field`, e nao a existencia do objeto: o ValidationPipe global de
    // main.ts roda com `transform: true` e instancia SortInput mesmo quando o argumento e
    // omitido, entregando um objeto truthy com os campos indefinidos.
    if (orderBy?.field) {
      this.assertColumnExists(table, orderBy.field, columns);
      this.assertNotEncrypted(table, orderBy.field, 'usada para ordenação');
    }

    const { joinSql, selectSql } = this.buildExpandJoins(table, columns, expand, 't', catalog);

    const params: unknown[] = [];
    const whereSql = this.buildWhereClause('t', where, params);
    const orderSql = orderBy?.field ? `ORDER BY t."${orderBy.field}" ${this.safeSortDirection(orderBy.direction)}` : '';

    // Diferente do POSH, "sem limit" nao significa "traz a tabela inteira".
    const effectiveLimit = this.clampLimit(limit, DEFAULT_TABLE_LIMIT, MAX_TABLE_LIMIT);
    params.push(effectiveLimit);
    const limitSql = `LIMIT $${params.length}`;

    let offsetSql = '';
    if (offset !== undefined && offset !== null) {
      if (offset < 0) {
        throw new BadRequestException('O "offset" não pode ser negativo.');
      }
      params.push(offset);
      offsetSql = `OFFSET $${params.length}`;
    }

    const sql = `
      SELECT t.*${selectSql} FROM "${ANALYTICS_SCHEMA}"."${table}" t
      ${joinSql}
      ${whereSql}
      ${orderSql}
      ${limitSql}
      ${offsetSql}
    `;

    const rows = await this.prisma.$queryRawUnsafe<Record<string, unknown>[]>(sql, ...params);
    const normalized = rows.map(row => this.normalizeRow(row));

    // Descriptografia sob demanda, so para as linhas desta pagina.
    await this.decryption.decryptRows(normalized, ENCRYPTED_COLUMNS[table] ?? []);

    return normalized;
  }

  @Query(() => [GraphQLJSONObject], {
    description:
      'Agrega uma tabela (GROUP BY + SUM/COUNT/AVG/MIN/MAX), com filtros opcionais e, se o campo de agrupamento for uma foreign key, junta colunas da tabela relacionada automaticamente. Não aceita SQL livre: tabela, colunas e função de agregação são sempre validadas contra o catálogo do banco. Métricas sensíveis são agregadas pela aplicação após a descriptografia; use "where" para restringir o volume lido.',
  })
  async reportAggregate(
    @Args('input', { type: () => ReportAggregateInput })
    input: ReportAggregateInput,
  ): Promise<Record<string, unknown>[]> {
    const { table, groupBy, metric, where, expand } = input;
    const orderBy = input.orderBy ?? SortDirection.DESC;
    const limit = this.clampLimit(input.limit, 20, MAX_AGGREGATE_LIMIT);

    const catalog = await this.getCatalog();
    this.assertTableExists(table, catalog);

    const columns = this.getColumnNames(table, catalog);
    this.assertColumnExists(table, groupBy, columns);
    this.assertNotEncrypted(table, groupBy, 'usada para agrupamento');
    this.assertColumnExists(table, metric.field, columns);
    for (const filter of where ?? []) {
      this.assertColumnExists(table, filter.field, columns);
      this.assertNotEncrypted(table, filter.field, 'filtrada');
    }

    let joinClause = '';
    const expandSelect: string[] = [];

    if (expand && expand.length > 0) {
      const relation = this.findOutgoingRelation(table, groupBy);

      if (!relation) {
        throw new BadRequestException(
          `A coluna "${groupBy}" de "${table}" não é uma foreign key conhecida, não é possível usar "expand".`,
        );
      }

      const relatedColumns = this.getColumnNames(relation.toTable, catalog);
      for (const column of expand) {
        this.assertColumnExists(relation.toTable, column, relatedColumns);
        this.assertNotEncrypted(relation.toTable, column, 'trazida por expand em agregação');
      }

      joinClause = `JOIN "${ANALYTICS_SCHEMA}"."${relation.toTable}" j ON g."${groupBy}" = j."${relation.toColumn}"`;
      for (const column of expand) {
        expandSelect.push(`j."${column}" AS "${column}"`);
      }
    }

    const params: unknown[] = [];
    const whereSql = this.buildWhereClause('g', where, params);

    // Teto de leitura: estourar e erro explicito, nunca truncamento silencioso.
    params.push(MAX_AGGREGATE_SCAN_ROWS + 1);
    const scanLimitIndex = params.length;

    const selectExpand = expandSelect.length > 0 ? `, ${expandSelect.join(', ')}` : '';

    const sql = `
      SELECT g."${groupBy}" AS "groupKey", g."${metric.field}" AS "metricSource"${selectExpand}
      FROM "${ANALYTICS_SCHEMA}"."${table}" g
      ${joinClause}
      ${whereSql}
      LIMIT $${scanLimitIndex}
    `;

    const rows = await this.prisma.$queryRawUnsafe<Record<string, unknown>[]>(sql, ...params);

    if (rows.length > MAX_AGGREGATE_SCAN_ROWS) {
      throw new BadRequestException(
        `A agregação precisaria ler mais de ${MAX_AGGREGATE_SCAN_ROWS} linhas de "${table}". ` +
          'Métricas sensíveis são agregadas pela aplicação, então restrinja o intervalo com "where" e tente de novo.',
      );
    }

    const normalized = rows.map(row => this.normalizeRow(row));

    if (this.isEncrypted(table, metric.field)) {
      const decrypted = await this.decryption.decryptColumn(normalized.map(row => row.metricSource as never));
      normalized.forEach((row, index) => {
        row.metricSource = decrypted[index];
      });
    }

    return this.aggregateInMemory(normalized, metric.op, expand ?? [], orderBy, limit);
  }

  // ---------------------------------------------------------------------------
  // Agregação em memória
  // ---------------------------------------------------------------------------

  private aggregateInMemory(
    rows: Record<string, unknown>[],
    op: AggregateFunction,
    expand: string[],
    direction: SortDirection,
    limit: number,
  ): Record<string, unknown>[] {
    const buckets = new Map<string, { row: Record<string, unknown>; values: number[]; nonNull: number }>();

    for (const row of rows) {
      const groupKey = row.groupKey ?? null;
      const bucketKey = JSON.stringify([groupKey, ...expand.map(column => row[column] ?? null)]);

      let bucket = buckets.get(bucketKey);
      if (!bucket) {
        const base: Record<string, unknown> = { groupKey };
        for (const column of expand) base[column] = row[column] ?? null;
        bucket = { row: base, values: [], nonNull: 0 };
        buckets.set(bucketKey, bucket);
      }

      const value = row.metricSource;
      if (value !== null && value !== undefined) {
        // COUNT conta nao-nulos de qualquer tipo (inclusive texto, como COUNT(id));
        // as demais funcoes so fazem sentido sobre valores numericos.
        bucket.nonNull += 1;

        const parsed = Number(value);
        if (Number.isFinite(parsed)) bucket.values.push(parsed);
      }
    }

    const result = Array.from(buckets.values()).map(bucket => ({
      ...bucket.row,
      metricValue: this.applyAggregate(op, bucket.values, bucket.nonNull),
    }));

    result.sort((a, b) => {
      const left = a.metricValue ?? Number.NEGATIVE_INFINITY;
      const right = b.metricValue ?? Number.NEGATIVE_INFINITY;
      return direction === SortDirection.ASC ? left - right : right - left;
    });

    return result.slice(0, limit);
  }

  /** COUNT conta valores nao nulos, como `COUNT(coluna)` no SQL -- inclusive nao numericos. */
  private applyAggregate(op: AggregateFunction, values: number[], nonNull: number): number | null {
    if (op === AggregateFunction.COUNT) return nonNull;
    if (values.length === 0) return null;

    switch (op) {
      case AggregateFunction.SUM:
        return values.reduce((total, value) => total + value, 0);
      case AggregateFunction.AVG:
        return values.reduce((total, value) => total + value, 0) / values.length;
      case AggregateFunction.MIN:
        return Math.min(...values);
      case AggregateFunction.MAX:
        return Math.max(...values);
      default:
        return null;
    }
  }

  // ---------------------------------------------------------------------------
  // SQL building
  // ---------------------------------------------------------------------------

  private buildWhereClause(alias: string, filters: FilterInput[] | undefined, params: unknown[]): string {
    const clauses: string[] = [];

    for (const filter of filters ?? []) {
      switch (filter.op) {
        case FilterOperator.IN:
          params.push(filter.value);
          clauses.push(`${alias}."${filter.field}" = ANY($${params.length})`);
          break;
        case FilterOperator.NOT_IN:
          params.push(filter.value);
          clauses.push(`${alias}."${filter.field}" <> ALL($${params.length})`);
          break;
        case FilterOperator.IS_NULL:
          clauses.push(`${alias}."${filter.field}" IS NULL`);
          break;
        case FilterOperator.IS_NOT_NULL:
          clauses.push(`${alias}."${filter.field}" IS NOT NULL`);
          break;
        default: {
          const sqlOperator = SQL_FILTER_OPERATOR[filter.op];

          if (!sqlOperator) {
            throw new BadRequestException(`Operador de filtro não suportado: "${filter.op}".`);
          }

          params.push(filter.value);
          clauses.push(`${alias}."${filter.field}" ${sqlOperator} $${params.length}`);
        }
      }
    }

    return clauses.length > 0 ? `WHERE ${clauses.join(' AND ')}` : '';
  }

  private buildExpandJoins(
    table: string,
    baseColumns: Set<string>,
    expand: ExpandInput[] | undefined,
    baseAlias: string,
    catalog: Catalog,
  ): { joinSql: string; selectSql: string } {
    if (!expand || expand.length === 0) {
      return { joinSql: '', selectSql: '' };
    }

    const joins: string[] = [];
    const selects: string[] = [];

    for (let i = 0; i < expand.length; i++) {
      const { viaColumn, columns } = expand[i];
      const alias = `e${i}`;

      this.assertColumnExists(table, viaColumn, baseColumns);

      const relation = this.findOutgoingRelation(table, viaColumn);
      if (!relation) {
        throw new BadRequestException(
          `A coluna "${viaColumn}" de "${table}" não é uma foreign key conhecida, não é possível usar "expand".`,
        );
      }

      const relatedColumns = this.getColumnNames(relation.toTable, catalog);
      for (const column of columns) {
        this.assertColumnExists(relation.toTable, column, relatedColumns);
      }

      joins.push(
        `LEFT JOIN "${ANALYTICS_SCHEMA}"."${relation.toTable}" ${alias} ON ${baseAlias}."${viaColumn}" = ${alias}."${relation.toColumn}"`,
      );

      for (const column of columns) {
        selects.push(`${alias}."${column}" AS "${viaColumn}__${column}"`);
      }
    }

    return {
      joinSql: joins.join('\n'),
      selectSql: selects.length > 0 ? `, ${selects.join(', ')}` : '',
    };
  }

  // ---------------------------------------------------------------------------
  // Catálogo (cacheado)
  // ---------------------------------------------------------------------------

  private async getCatalog(): Promise<Catalog> {
    if (this.catalogCache && Date.now() - this.catalogCache.loadedAt < CATALOG_TTL_MS) {
      return this.catalogCache;
    }

    if (this.catalogInFlight !== null) return this.catalogInFlight;

    this.catalogInFlight = this.loadCatalog();

    try {
      this.catalogCache = await this.catalogInFlight;
      return this.catalogCache;
    } finally {
      this.catalogInFlight = null;
    }
  }

  private async loadCatalog(): Promise<Catalog> {
    const [tableRows, columnRows] = await Promise.all([
      this.prisma.$queryRawUnsafe<{ table_name: string; description: string | null }[]>(
        `SELECT t.table_name,
                obj_description(format('%I.%I', t.table_schema, t.table_name)::regclass, 'pg_class') AS description
         FROM information_schema.tables t
         WHERE t.table_schema = $1
           AND t.table_type IN ('BASE TABLE', 'VIEW')
         ORDER BY t.table_name`,
        ANALYTICS_SCHEMA,
      ),
      this.prisma.$queryRawUnsafe<
        {
          table_name: string;
          column_name: string;
          data_type: string;
          is_nullable: string;
          description: string | null;
        }[]
      >(
        `SELECT c.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                col_description(format('%I.%I', c.table_schema, c.table_name)::regclass::oid,
                                c.ordinal_position) AS description
         FROM information_schema.columns c
         JOIN information_schema.tables t
           ON t.table_schema = c.table_schema
          AND t.table_name = c.table_name
         WHERE c.table_schema = $1
           AND t.table_type IN ('BASE TABLE', 'VIEW')
         ORDER BY c.table_name, c.ordinal_position`,
        ANALYTICS_SCHEMA,
      ),
    ]);

    const tableDescriptions = new Map<string, string | null>();
    for (const row of tableRows) {
      tableDescriptions.set(row.table_name, row.description);
    }

    const columnsByTable = new Map<string, ColumnMeta[]>();
    for (const row of columnRows) {
      const list = columnsByTable.get(row.table_name) ?? [];
      list.push({
        column: row.column_name,
        dataType: row.data_type,
        nullable: row.is_nullable === 'YES',
        description: row.description,
      });
      columnsByTable.set(row.table_name, list);
    }

    return {
      tables: tableRows.map(row => row.table_name),
      tableDescriptions,
      columnsByTable,
      loadedAt: Date.now(),
    };
  }

  private findOutgoingRelation(table: string, column: string): { toTable: string; toColumn: string } | null {
    const relation = ANALYTICS_RELATIONS.find(item => item.fromTable === table && item.fromColumn === column);

    return relation ? { toTable: relation.toTable, toColumn: relation.toColumn } : null;
  }

  // ---------------------------------------------------------------------------
  // Validação e normalização
  // ---------------------------------------------------------------------------

  private isEncrypted(table: string, column: string): boolean {
    return (ENCRYPTED_COLUMNS[table] ?? []).includes(column);
  }

  /**
   * Colunas sensiveis chegam do banco cifradas, entao o Postgres nao pode compara-las nem
   * ordena-las. Recusar e obrigatorio: deixar passar produziria resultado errado em silencio.
   */
  private assertNotEncrypted(table: string, column: string, purpose: string): void {
    if (this.isEncrypted(table, column)) {
      throw new BadRequestException(
        `A coluna "${column}" de "${table}" é sensível e fica cifrada no banco, portanto não pode ser ${purpose}. ` +
          'Ela pode ser lida e usada como métrica em reportAggregate.',
      );
    }
  }

  private describeColumn(table: string, meta: ColumnMeta): string | null {
    if (!this.isEncrypted(table, meta.column)) return meta.description;

    const suffix =
      'Coluna sensível: cifrada no banco e decifrada na consulta. Não aceita filtro nem ordenação; pode ser usada como métrica em reportAggregate.';

    return meta.description ? `${meta.description} ${suffix}` : suffix;
  }

  private assertTableExists(table: string, catalog: Catalog): void {
    if (!catalog.tables.includes(table)) {
      throw new BadRequestException(`Tabela "${table}" não encontrada no schema ${ANALYTICS_SCHEMA}.`);
    }
  }

  private getColumnNames(table: string, catalog: Catalog): Set<string> {
    return new Set((catalog.columnsByTable.get(table) ?? []).map(meta => meta.column));
  }

  private assertColumnExists(table: string, column: string, columns: Set<string>): void {
    if (!columns.has(column)) {
      throw new BadRequestException(`Coluna "${column}" não encontrada na tabela "${table}".`);
    }
  }

  /**
   * A direcao e interpolada no SQL, entao passa por allowlist em vez de confiar apenas na
   * validacao do enum GraphQL.
   */
  private safeSortDirection(direction: SortDirection | undefined): string {
    return direction === SortDirection.ASC ? 'ASC' : 'DESC';
  }

  private clampLimit(limit: number | null | undefined, fallback: number, max: number): number {
    if (limit === undefined || limit === null) return fallback;

    if (!Number.isInteger(limit) || limit < 1) {
      throw new BadRequestException('O "limit" deve ser um inteiro maior ou igual a 1.');
    }

    if (limit > max) {
      throw new BadRequestException(`O "limit" excede o máximo permitido (${max}).`);
    }

    return limit;
  }

  /**
   * O Postgres devolve COUNT como bigint e numeric como string/Decimal. Sem isto, o bigint
   * quebra a serialização JSON e as métricas voltam como texto.
   */
  private normalizeRow(row: Record<string, unknown>): Record<string, unknown> {
    const normalized: Record<string, unknown> = {};

    for (const [key, value] of Object.entries(row)) {
      normalized[key] = this.normalizeValue(value);
    }

    return normalized;
  }

  private normalizeValue(value: unknown): unknown {
    if (typeof value === 'bigint') {
      return Number(value);
    }

    if (
      value !== null &&
      typeof value === 'object' &&
      typeof (value as { toNumber?: unknown }).toNumber === 'function'
    ) {
      return (value as { toNumber: () => number }).toNumber();
    }

    return value;
  }
}