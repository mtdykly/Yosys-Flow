module demo(input clk, rst_n, en, a, b, c, d, output y, z, output reg q, output reg [1:0] r);
assign y = a & b;
assign z = c ^ d;
wire next;
assign next = en ? d : q;
always @(posedge clk) q <= next;
always @(posedge clk or negedge rst_n)
    if (!rst_n) r <= 0;
    else begin
        r[0] <= a;
        r[1] <= b;
    end
endmodule
