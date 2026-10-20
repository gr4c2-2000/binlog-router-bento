# Router CDC w Go: binlogi MySQL przez Bento na wiele sinków

Problem jest banalny do opisania i uciążliwy do zrobienia dobrze: zmiany w bazie
MySQL mają trafić do kilku systemów naraz, każdy z innym wycinkiem danych.
Kafka ma dostać tylko zamówienia, audyt tylko transakcje powyżej progu, archiwum
na S3 wszystko. Klasyczne podejście — triggery, polling po `updated_at`, kolejka
pisana z aplikacji — albo obciąża bazę, albo gubi zdarzenia, albo wymaga, żeby
każdy producent pamiętał o publikowaniu eventów.

Czysty CDC rozwiązuje to inaczej: czytamy binlog MySQL, czyli ten sam strumień,
z którego korzystają repliki, i rozsyłamy go dalej. Poniżej składam taki router
z dwóch klocków — `go-mysql` jako czytnika binloga i Bento jako silnika
routingu.

## 1. Czym jest Bento

[Bento](https://github.com/warpstreamlabs/bento) to procesor strumieni
deklarowany w YAML-u — fork Benthosa, utrzymywany przez WarpStream Labs po tym,
jak oryginalny projekt trafił pod Redpandę i zmienił licencję. Jeden statycznie
linkowany binarek, bez JVM, bez klastra, bez ZooKeepera.

Model jest prosty i to jest jego największa zaleta:

```yaml
input:      # skąd
pipeline:   # co z tym zrobić
  processors: []
output:     # dokąd
```

W komplecie jest ponad sto komponentów: Kafka, NATS, Redis, SQL, S3, GCP
Pub/Sub, HTTP, MQTT, Pulsar, pliki, stdin. Transformacje pisze się w Bloblangu —
własnym języku mapowania, który zastępuje typowe „wrzućmy tu lambdę w Pythonie":

```coffee
root.id = this.user.id
root.total = this.items.map_each(i -> i.price * i.qty).sum()
root = if this.status == "draft" { deleted() }
```

`deleted()` kasuje wiadomość ze strumienia — to jest cały mechanizm filtrowania
i za chwilę zrobi nam całą robotę przy routingu.

Bento ma wbudowany backpressure, retry, batching, buforowanie na dysku i
metryki. Daje gwarancję at-least-once: wiadomość jest potwierdzana wobec
źródła dopiero wtedy, gdy wyjście potwierdzi zapis.

Dla nas najważniejsze jest jednak to, że Bento to **biblioteka Go**, nie tylko
CLI. Pakiet `github.com/warpstreamlabs/bento/public/service` pozwala
zarejestrować własny input, procesor albo output i zbudować z tego własny
binarek, który dalej rozumie standardowy YAML i wszystkie wbudowane komponenty.

## 2. Co daje go-mysql — binlog jako strumień zdarzeń

[go-mysql](https://github.com/go-mysql-org/go-mysql) to czysto-go'wa
implementacja protokołu sieciowego MySQL, łącznie z protokołem replikacji.
Biblioteka podszywa się pod replikę: łączy się z masterem, mówi „jestem serwerem
o ID 1001, poproszę binlog od pozycji X" i dostaje strumień zdarzeń.

Są dwa poziomy API.

**`replication.BinlogSyncer`** — surowe zdarzenia binloga, jeden do jednego z
protokołem:

```go
cfg := replication.BinlogSyncerConfig{
    ServerID: 100,
    Flavor:   "mysql",
    Host:     "127.0.0.1",
    Port:     3306,
    User:     "repl",
    Password: "repl",
}
syncer := replication.NewBinlogSyncer(cfg)
streamer, _ := syncer.StartSync(mysql.Position{Name: binlogFile, Pos: binlogPos})

for {
    ev, _ := streamer.GetEvent(context.Background())
    ev.Dump(os.Stdout)
}
```

Dostajesz `RotateEvent`, `TableMapEvent`, `WriteRowsEvent`, `XIDEvent` i resztę
zoo. Pełna kontrola, ale metadane tabel — nazwy kolumn, typy — musisz śledzić
sam, bo w surowym binlogu wierszy są same wartości pozycyjne.

**`canal`** — warstwa wyżej, i to jej zwykle chcesz:

```go
type MyEventHandler struct {
    canal.DummyEventHandler
}

func (h *MyEventHandler) OnRow(e *canal.RowsEvent) error {
    log.Infof("%s %v\n", e.Action, e.Rows)
    return nil
}

cfg := canal.NewDefaultConfig()
cfg.Addr = "127.0.0.1:3306"
cfg.Dump.Tables = []string{"canal_test"}

c, _ := canal.NewCanal(cfg)
c.SetEventHandler(&MyEventHandler{})
c.Run()
```

Canal sam utrzymuje połączenie, obsługuje reconnect i rotację plików binloga,
a przede wszystkim trzyma cache schematu: w `e.Table.Columns` masz nazwy kolumn,
więc możesz zamienić pozycyjny wiersz w sensownego JSON-a. Filtruje też tabele
regexpem (`IncludeTableRegex`) i potrafi zrobić wstępny snapshot przez
`mysqldump`, zanim przejdzie na strumień.

Kilka rzeczy, o których trzeba wiedzieć od razu:

- Przy `UpdateAction` pole `e.Rows` zawiera **pary**: wiersz przed i wiersz po,
  naprzemiennie. Łatwo to przeoczyć i wypuścić dwa razy więcej eventów.
- Baza musi mieć `binlog_format=ROW`. Bez `binlog_row_image=FULL` stan sprzed
  zmiany będzie niekompletny.
- Użytkownik potrzebuje grantów `REPLICATION SLAVE` i `REPLICATION CLIENT`.
- `ServerID` musi być unikalny w całej topologii replikacji. Dwa procesy z tym
  samym ID będą się nawzajem rozłączać.
- Pozycji w binlogu **nikt za ciebie nie zapisze**. Canal jej nie utrwala — to
  twoja robota i od niej zależy, czy restart oznacza wznowienie, czy utratę
  zdarzeń.

## 3. Sklejenie: własny input plugin

Bento nie ma wbudowanego inputu MySQL CDC — na liście komponentów jest
CockroachDB changefeed i GCP Spanner CDC, MySQL-a nie ma. Piszemy własny.

Kontrakt jest króciutki:

```go
type Input interface {
    Connect(ctx context.Context) error
    Read(ctx context.Context) (*Message, AckFunc, error)
    Close(ctx context.Context) error
}
```

Problem do rozwiązania jest jeden: canal jest **push** (woła twój handler),
a Bento jest **pull** (woła twoje `Read`). Mostem jest kanał — i to kanał
*bez bufora*, bo wtedy blokująca wysyłka w handlerze canala staje się naturalnym
backpressure. Gdy pipeline stanie, czytanie binloga też stanie, zamiast puchnąć
w pamięci.

### Handler canala: wiersz na wiadomość

```go
type event struct {
    Schema string         `json:"schema"`
    Table  string         `json:"table"`
    Action string         `json:"action"` // insert | update | delete
    Before map[string]any `json:"before,omitempty"`
    After  map[string]any `json:"after,omitempty"`
    File   string         `json:"file"`
    Pos    uint32         `json:"pos"`
}

type handler struct {
    canal.DummyEventHandler
    out  chan *service.Message
    ctx  context.Context
    mu   sync.Mutex
    file string
}

// The binlog file name only arrives in rotate events, so we track it here.
func (h *handler) OnRotate(_ *replication.EventHeader, r *canal.RotateEvent) error {
    h.mu.Lock()
    h.file = string(r.NextLogName)
    h.mu.Unlock()
    return nil
}

func rowMap(t *schema.Table, row []any) map[string]any {
    m := make(map[string]any, len(row))
    for i, c := range t.Columns {
        if i < len(row) {
            m[c.Name] = row[i]
        }
    }
    return m
}

func (h *handler) OnRow(e *canal.RowsEvent) error {
    base := event{
        Schema: e.Table.Schema,
        Table:  e.Table.Name,
        Action: e.Action,
        File:   h.curFile(),
        Pos:    e.Header.LogPos,
    }

    var evs []event
    switch e.Action {
    case canal.UpdateAction:
        // Rows arrive in before/after pairs.
        for i := 0; i+1 < len(e.Rows); i += 2 {
            ev := base
            ev.Before = rowMap(e.Table, e.Rows[i])
            ev.After = rowMap(e.Table, e.Rows[i+1])
            evs = append(evs, ev)
        }
    case canal.DeleteAction:
        for _, r := range e.Rows {
            ev := base
            ev.Before = rowMap(e.Table, r)
            evs = append(evs, ev)
        }
    default: // insert
        for _, r := range e.Rows {
            ev := base
            ev.After = rowMap(e.Table, r)
            evs = append(evs, ev)
        }
    }

    for _, ev := range evs {
        msg := service.NewMessage(nil)
        msg.SetStructured(ev)
        msg.MetaSet("db", ev.Schema)
        msg.MetaSet("table", ev.Table)
        msg.MetaSet("action", ev.Action)
        msg.MetaSet("binlog_file", ev.File)
        msg.MetaSet("binlog_pos", strconv.FormatUint(uint64(ev.Pos), 10))

        // Blocking send is the backpressure.
        select {
        case h.out <- msg:
        case <-h.ctx.Done():
            return h.ctx.Err()
        }
    }
    return nil
}
```

Warto zwrócić uwagę na metadane. Dublują treść, ale dzięki nim filtry w
konfiguracji są jednolinijkowe i nie trzeba parsować payloadu — `meta("table")`
zamiast `this.table`.

### Input: Connect, Read, Close

```go
func (b *binlogInput) Connect(ctx context.Context) error {
    c, err := canal.NewCanal(b.cfg)
    if err != nil {
        return err
    }

    runCtx, cancel := context.WithCancel(context.Background())
    b.c, b.cancel = c, cancel
    b.out = make(chan *service.Message)
    b.errCh = make(chan error, 1)

    c.SetEventHandler(&handler{out: b.out, ctx: runCtx})

    pos, err := b.loadPos()
    if err != nil {
        cancel()
        return err
    }

    go func() {
        defer close(b.out)
        var runErr error
        if pos.Name == "" {
            runErr = c.Run() // start from the current master position
        } else {
            runErr = c.RunFrom(pos)
        }
        if runErr != nil {
            select {
            case b.errCh <- runErr:
            default:
            }
        }
    }()
    return nil
}

func (b *binlogInput) Read(ctx context.Context) (*service.Message, service.AckFunc, error) {
    select {
    case msg, ok := <-b.out:
        if !ok {
            select {
            case err := <-b.errCh:
                return nil, nil, err
            default:
            }
            return nil, nil, service.ErrNotConnected
        }
        file, _ := msg.MetaGet("binlog_file")
        posStr, _ := msg.MetaGet("binlog_pos")

        ack := func(_ context.Context, err error) error {
            if err != nil {
                return nil // nack: do not advance the offset
            }
            p, _ := strconv.ParseUint(posStr, 10, 32)
            return b.savePos(mysql.Position{Name: file, Pos: uint32(p)})
        }
        return msg, ack, nil

    case err := <-b.errCh:
        return nil, nil, err
    case <-ctx.Done():
        return nil, nil, ctx.Err()
    }
}

func (b *binlogInput) Close(ctx context.Context) error {
    if b.cancel != nil {
        b.cancel()
    }
    if b.c != nil {
        b.c.Close()
    }
    return nil
}
```

Sedno jest w `AckFunc`. Bento woła ją dopiero wtedy, gdy wiadomość została
dostarczona do wszystkich wyjść. Zapisujemy pozycję binloga wyłącznie w tym
momencie, więc restart wznawia od ostatniego faktycznie dostarczonego zdarzenia.
Przy nacku nie robimy nic — pozycja zostaje tam, gdzie była.

Jedno zastrzeżenie, bo to pułapka: acki niekoniecznie przychodzą w kolejności
wysyłki. Powyższy kod zapisuje ostatnią potwierdzoną pozycję, a nie *watermark*
najniższej niepotwierdzonej. W produkcji trzymaj kopiec niepotwierdzonych
pozycji i commituj dopiero ciągły prefiks. Semantyka i tak pozostaje
at-least-once — wszystkie sinki muszą być idempotentne.

### Rejestracja

```go
func main() {
    spec := service.NewConfigSpec().
        Summary("Reads MySQL row events from the binary log.").
        Field(service.NewStringField("addr").Default("127.0.0.1:3306")).
        Field(service.NewStringField("user")).
        Field(service.NewStringField("password").Secret()).
        Field(service.NewIntField("server_id").Default(1001)).
        Field(service.NewStringListField("include_tables").Default([]any{})).
        Field(service.NewStringField("pos_file").Default("./binlog.pos"))

    ctor := func(conf *service.ParsedConfig, mgr *service.Resources) (service.Input, error) {
        cfg := canal.NewDefaultConfig()
        // ... read fields into cfg ...
        cfg.Dump.ExecutionPath = "" // binlog only, no mysqldump snapshot
        return &binlogInput{cfg: cfg, posFile: posFile}, nil
    }

    service.RegisterInput("mysql_binlog", spec, ctor)
    service.RunCLI(context.Background())
}
```

Plus pusty import `_ "github.com/warpstreamlabs/bento/public/components/all"`,
żeby wciągnąć wszystkie standardowe komponenty. Od tego momentu `mysql_binlog`
jest normalnym inputem, równoprawnym z `kafka` czy `file`, z walidacją
konfiguracji i dokumentacją generowaną z `ConfigSpec`.

## 4. Rozrzucanie jednego strumienia na wiele sinków

I tu Bento odpłaca się za całą tę robotę, bo routing to już tylko YAML.

Są dwa wzorce i mylenie ich to najczęstszy błąd.

**`switch`** — routing rozłączny. Sprawdzane po kolei, wygrywa pierwszy pasujący
`check`, wiadomość trafia dokładnie w jedno miejsce. Dobry do dispatchu po typie.

**`broker` z `pattern: fan_out`** — każde wyjście dostaje **kopię** każdej
wiadomości i filtruje sobie samo. Filtry mogą się dowolnie nakładać. Tego
chcemy przy routerze CDC, bo S3 ma archiwizować również to, co poszło na Kafkę.

```yaml
input:
  mysql_binlog:
    addr: 127.0.0.1:3306
    user: repl
    password: "${MYSQL_REPL_PASSWORD}"
    server_id: 1001
    include_tables: [ "shop\\..*" ]
    pos_file: /var/lib/binlog-router/binlog.pos

output:
  broker:
    pattern: fan_out
    outputs:
      # 1. Kafka — tylko zamówienia.
      - processors:
          - mapping: root = if meta("table") != "orders" { deleted() }
        kafka:
          addresses: [ "localhost:9092" ]
          topic: orders-cdc
          key: ${! json("after.id") }

      # 2. Audyt — tylko transakcje powyżej progu, bez usunięć.
      - processors:
          - mapping: |
              root = if meta("action") == "delete" { deleted() }
              root = if this.after.amount.or(0) < 1000 { deleted() }
        http_client:
          url: https://audit.internal/events
          verb: POST

      # 3. Archiwum — wszystko, co ze schematu shop.
      - processors:
          - mapping: root = if meta("db") != "shop" { deleted() }
        aws_s3:
          bucket: cdc-archive
          path: ${!meta("table")}/${!timestamp_unix()}-${!uuid_v4()}.json
```

Procesory zagnieżdżone pod wyjściem działają tylko na kopii tego wyjścia, więc
każdy sink może też przekształcić payload pod swój format — Kafka dostaje
pełnego JSON-a, audyt spłaszczony rekord, S3 surowy event.

### Co trzeba wiedzieć o fan_out

Potwierdzenie przychodzi dopiero, gdy **wszystkie** wyjścia potwierdzą zapis.
To dokładnie ta własność, która sprawia, że zapis pozycji binloga w `AckFunc`
jest bezpieczny — nie przesuniemy offsetu, dopóki event nie wyląduje wszędzie.

Rewers tego medalu: jeden padnięty sink blokuje cały router i wymusza retry
także na tych, które zapisały poprawnie. Stąd dwie decyzje projektowe:

- daj każdemu wyjściu własną politykę `retry` i kolejkę martwych listów, żeby
  chwilowo niedostępny endpoint HTTP nie zatrzymywał Kafki;
- jeśli zależy ci na kolejności prób, użyj `fan_out_sequential`.

Przy wolnych albo kapryśnych sinkach wstaw `buffer` — Bento ma bufor na dysku —
ale świadomie: bufor przesuwa moment acku, więc rozluźnia gwarancję, za którą
płacimy tym całym mechanizmem pozycji.

### Czego nie da się obejść

Router jest z definicji **jednoprocesowy**. Jeden czytnik na jeden `server_id`,
inaczej tracisz kolejność i dublujesz zdarzenia. Równoległość zaczyna się
dopiero za Bento — partycje Kafki, workerzy po stronie konsumenta. Skalowanie
poziome samego czytnika binloga to nie jest problem, który rozwiążesz
konfiguracją.

## Podsumowanie

Całość to jakieś 200 linii Go i kilkanaście linii YAML-a. `go-mysql` daje
wiarygodny strumień zmian prosto z protokołu replikacji, Bento daje routing,
retry, backpressure i sto gotowych wyjść, a własny input plugin to po prostu
adapter między push-owym handlerem canala a pull-owym `Read`.

Dwie rzeczy, na których ten projekt stoi albo się przewraca: utrwalanie pozycji
binloga dokładnie w momencie acku i idempotentność sinków. Reszta to konfiguracja.
