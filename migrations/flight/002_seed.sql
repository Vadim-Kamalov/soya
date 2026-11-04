INSERT INTO airlines VALUES ('SU','Аэрофлот'),('DP','Победа');
INSERT INTO airports VALUES ('SVO','Шереметьево'),('VKO','Внуково'),('LED','Пулково');
INSERT INTO flights(id,flight_number,airline_code,origin,destination,departure_at,arrival_at,total_seats,available_seats,price,status) VALUES
('11111111-1111-4111-8111-111111111111','SU1234','SU','SVO','LED','2026-12-01T10:00:00Z','2026-12-01T11:30:00Z',100,100,12500.00,'SCHEDULED'),
('22222222-2222-4222-8222-222222222222','DP201','DP','VKO','LED','2026-12-01T12:00:00Z','2026-12-01T13:30:00Z',50,50,7500.00,'SCHEDULED'),
('33333333-3333-4333-8333-333333333333','SU1234','SU','SVO','LED','2026-12-02T10:00:00Z','2026-12-02T11:30:00Z',100,100,13000.00,'SCHEDULED');

